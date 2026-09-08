from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from ..models import Account, ErrorCode, SparkContact, SparkScanResult, SparkScanStatus, SparkState
from .douyin_chat import _CONVERSATION_IDENTITY_JS, DouyinChatAdapter
from .errors import AutomationError

RECOGNITION_LIMIT = (
    "严格识别使用聊天行内显式徽章语义，或经公开客户端源码核对的专用火花元数据及有效时间窗；"
    "LIGHT/GRAY 均为有效火花，RECOVER 单列待恢复，LIGHT_DOWN 为已熄灭。"
    "不根据颜色、SVG、昵称、消息火焰或单独的天数/图标资产判断。"
    "火花状态仅供参考；身份可靠且类型明确的单聊均可手动勾选导入，新增目标保持停用。"
    "无法确认火花状态时保留待确认；稳定身份或单聊类型未确认时不可导入。"
    "结果不代表账号全部火花好友。"
)

# List/name/header classes were observed in an authorized metadata-only DOM probe.
# Badge state/identity are still independent evidence gates; no row/body HTML or
# message preview text leaves the page.
_SNAPSHOT_JS = r"""({operation, limit}) => {
    const visible = element => {
        const r = element.getBoundingClientRect(), s = getComputedStyle(element);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
            && s.display !== 'none';
    };
    const clean = value => String(value || '').replace(/[\s\u200B-\u200D\uFEFF]+/g, ' ').trim();
    const listSelector = '.conversationConversationListwrapper, [role="list"], [role="listbox"], ul, ol';
    const rowSelector = '.conversationConversationItemwrapper, [role="listitem"], [role="option"], li';
    const headerSelector = '.conversationConversationItemtitleWrapper, .ConversationItemTagNextToTitlewrapper, [data-role="conversation-header"]';
    const previewSelector = '.ConversationItemDescwrapper, .ConversationItemHinttextBox, [data-role="message-preview"], [data-role="message"]';
    const observedLists = [...document.querySelectorAll('.conversationConversationListwrapper')].filter(visible);
    let list, panel;
    if (observedLists.length === 1) {
        list = observedLists[0];
        panel = document.body;
    } else {
    if (observedLists.length > 1) return {found: false, rows: []};
    const inputs = [...document.querySelectorAll('input')].filter(element =>
        visible(element) && (element.placeholder || '').includes('搜索')
        && element.getAttribute('data-e2e') !== 'searchbar-input');
    if (inputs.length !== 1) return {found: false, rows: []};
    const search = inputs[0], searchRect = search.getBoundingClientRect();
    // Same bounded left search pane convention as DouyinChatAdapter.
    panel = null;
    for (let node = search.parentElement; node && node !== document.body; node = node.parentElement) {
        const r = node.getBoundingClientRect();
        if (visible(node) && r.width >= searchRect.width && r.width <= innerWidth * .48
            && r.height >= innerHeight * .35 && r.left <= searchRect.left + 24
            && r.right >= searchRect.right - 24) { panel = node; break; }
    }
    if (!panel) return {found: false, rows: []};
    const lists = [...panel.querySelectorAll('[role="list"], [role="listbox"], ul, ol')]
        .filter(element => visible(element)
            && /^(聊天列表|会话列表|私信列表|Chat list|Conversation list)$/i.test(
                clean(element.getAttribute('aria-label'))));
    if (lists.length !== 1) return {found: false, rows: []};
    list = lists[0];
    }
    let scroller = list;
    for (let node = list; node && panel.contains(node); node = node.parentElement) {
        if (node.scrollHeight > node.clientHeight && /(auto|scroll)/.test(getComputedStyle(node).overflowY)) {
            scroller = node; break;
        }
    }
    // Virtual lists can put their scroll viewport below the semantic list root.
    const mountedRows = [...list.querySelectorAll(rowSelector)];
    const nestedScrollers = [...list.querySelectorAll('*')].filter(node =>
        visible(node) && node.scrollHeight > node.clientHeight
        && /(auto|scroll)/.test(getComputedStyle(node).overflowY)
        && mountedRows.length > 0 && mountedRows.every(row => node.contains(row)));
    if (nestedScrollers.length) scroller = nestedScrollers[nestedScrollers.length - 1];
    if (operation === 'reset') {
        scroller.scrollTop = 0;
        return {found: true, rows: []};
    }
    if (operation === 'scroll') {
        scroller.scrollTop += Math.max(32, scroller.clientHeight * .75);
        return {found: true, rows: []};
    }
    const box = scroller.getBoundingClientRect();
    const inView = element => {
        const r = element.getBoundingClientRect();
        return visible(element) && r.bottom > box.top && r.top < box.bottom
            && r.bottom > 0 && r.top < innerHeight;
    };
    const rows = [...list.querySelectorAll(rowSelector)]
        .filter(row => row.closest(listSelector) === list && inView(row));
    const identityAttributes = ['data-uid', 'data-user-id', 'data-conversation-id'];
    const result = rows.slice(0, limit).map((row, index) => {
        // Names/identities must be in a dedicated header/name node, never the whole row.
        const names = [...row.querySelectorAll('.conversationConversationItemtitle, [data-role="conversation-name"], [role="heading"]')]
            .filter(element => visible(element) && element.closest(headerSelector)
                && !element.closest(previewSelector));
        const nameNode = names.length === 1 ? names[0] : null;
        const name = nameNode && (nameNode.children.length === 0 || nameNode.matches('.conversationConversationItemtitle'))
            ? clean(nameNode.textContent).slice(0, 160) : '';
        const identityNodes = [row, ...(nameNode ? [nameNode, ...nameNode.querySelectorAll('a[href]')] : [])];
        const links = [...row.querySelectorAll('a[href]')].filter(link =>
            visible(link) && link.closest(headerSelector)
                && !link.closest(previewSelector)
                && (link === nameNode || nameNode?.contains(link)
                || link.contains(nameNode) || link.getAttribute('aria-label') === '查看主页'));
        const profiles = [...new Set(links.map(link => link.href))];
        const conversationIdentity = (__READ_CONVERSATION_IDENTITY__)(row, true);
        if (conversationIdentity?.secUserId) {
            profiles.push(`https://www.douyin.com/user/${conversationIdentity.secUserId}`);
        }
        const ids = [...new Set(identityNodes.map(node => clean(node.getAttribute('data-douyin-id')))
            .filter(value => value && value.length <= 100))];
        const dataIds = identityAttributes.flatMap(key => {
            const value = clean(row.getAttribute(key));
            return value && value.length <= 200 ? [`${key}:${value}`] : [];
        });
        const types = [row.getAttribute('data-conversation-type'), row.getAttribute('data-chat-type')]
            .map(clean).filter(Boolean);
        // Public SDK 4187.2096e141.js: ONE_TO_ONE_CHAT=1, GROUP_CHAT=2.
        if (conversationIdentity && conversationIdentity.type !== null) {
            types.push(conversationIdentity.type === 1 ? 'single'
                : conversationIdentity.type === 2 ? 'group' : 'unknown');
        }
        const typeLabels = [...row.querySelectorAll('[role="img"], img')]
            .filter(node => visible(node) && node.closest(headerSelector)
                && !node.closest(previewSelector))
            .map(node => clean(node.getAttribute('aria-label') || node.getAttribute('alt')))
            .filter(label => ['单聊', '群聊'].includes(label));
        // An explicit accessible image/status label identifies a dedicated badge.
        // Generic title spans, row names, preview text and bare flame emoji are not evidence.
        const badges = [...row.querySelectorAll('[role="img"], img, [role="status"]')]
            .filter(node => visible(node) && !nameNode?.contains(node)
                && node.closest(headerSelector)
                && !node.closest(previewSelector))
            .flatMap(node => ['aria-label', 'alt', 'title'].map(attribute => clean(node.getAttribute(attribute))))
            .filter(value => value && value.length <= 80 && /火花|spark|streak/i.test(value));
        const flameBadges = [...row.querySelectorAll('.commonStreakstreakContainer')]
            .filter(node => visible(node) && node.closest(headerSelector) && !node.closest(previewSelector));
        return {name, profiles, ids, dataId: dataIds.sort().join('|'),
            types: [...types, ...typeLabels], badges, slot: index,
            scanConversationId: conversationIdentity?.scanConversationId || '',
            flame: conversationIdentity?.flame || null, flameVisible: flameBadges.length === 1};
    });
    const loading = list.getAttribute('aria-busy') === 'true'
        || [...list.querySelectorAll('[role="progressbar"]')].some(inView);
    // Dedicated list-local status is required; scroll geometry alone is never an end marker.
    const end = !loading && [...list.querySelectorAll('[role="status"]')].some(node =>
        inView(node) && !node.closest(rowSelector)
            && /^(会话列表已全部加载|聊天列表已全部加载|没有更多会话|没有更多聊天|End of conversations)$/i
            .test(clean(node.getAttribute('aria-label'))));
    return {found: true, rows: result, top: scroller.scrollTop, loading, end,
        overflow: rows.length > limit};
}""".replace("__READ_CONVERSATION_IDENTITY__", _CONVERSATION_IDENTITY_JS)


def _profile_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"douyin.com", "www.douyin.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not re.fullmatch(r"/user/[A-Za-z0-9_-]+/?", parsed.path)
            or parsed.path.rstrip("/").endswith("/self")
        ):
            return ""
        # Keep the adapter's canonical host/key convention.
        return f"https://{parsed.netloc}{parsed.path.rstrip('/')}"
    except ValueError:
        return ""


def _contact(raw: dict, observation: int) -> SparkContact:
    profiles = {_profile_url(value) for value in raw["profiles"]}
    invalid_profile = "" in profiles
    profiles.discard("")
    ids = set(raw["ids"])
    invalid_id = any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) for value in ids)
    conflict = len(profiles) > 1 or len(ids) > 1 or invalid_profile or invalid_id
    candidate = DouyinChatAdapter._candidate_from_raw(
        {
            "name": raw["name"] or "名称待确认",
            "profileUrl": next(iter(profiles)) if len(profiles) == 1 and not conflict else "",
            "douyinId": next(iter(ids)) if len(ids) == 1 and not conflict else "",
            "dataId": raw["dataId"],
        }
    )
    types = {str(value).casefold() for value in raw["types"]}
    singles = {"single", "private", "direct", "单聊"}
    groups = {"group", "群聊"}
    is_group = bool(types & groups)
    conversation_type = (
        "group" if is_group else "single" if types and types <= singles else "unknown"
    )
    strong = (
        not conflict and bool(candidate.profile_url or candidate.douyin_id) and bool(raw["name"])
    )
    evidence = dict(candidate.evidence)
    evidence.update(
        identity_strength="strong" if strong else "weak",
        chat_type=conversation_type,
        capture_source="spark_scan",
        spark_evidence="explicit_accessible_badge",
        identity_conflict=conflict,
    )
    scan_id = str(raw.pop("scanConversationId", "") or "")
    if scan_id:
        evidence["scan_conversation_digest"] = hashlib.sha256(scan_id.encode("utf-8")).hexdigest()
    candidate = replace(candidate, evidence=evidence)
    if not strong:
        # Never collapse distinct weak rows merely because their names/avatars match.
        candidate = replace(candidate, stable_key=f"pending:{observation}:{candidate.stable_key}")
    labels = {str(value).casefold() for value in raw["badges"]}
    active_labels = {
        "火花已点亮",
        "有效火花",
        "火花持续中",
        "火花未熄灭",
        "灰色有效火花",
        "灰色火花",
        "火花已置灰",
        "火花置灰",
        "spark active",
        "streak active",
        "spark gray",
        "streak gray",
    }
    recover_labels = {
        "火花恢复中",
        "火花待恢复",
        "火花可恢复",
        "火花重燃中",
        "spark recover",
        "streak recover",
    }
    inactive_labels = {
        "火花已熄灭",
        "火花已失效",
        "火花未点亮",
        "spark inactive",
        "streak inactive",
    }
    active = bool(labels & active_labels)
    recover = bool(labels & recover_labels)
    inactive = bool(labels & inactive_labels)
    # Unrecognized/conflicting badge semantics cannot be overruled by another label.
    uncertain = bool(labels - active_labels - recover_labels - inactive_labels)
    if active and not recover and not inactive and not uncertain:
        state, reason = SparkState.ACTIVE, "专用徽章明确标注有效火花"
    elif recover and not active and not inactive and not uncertain:
        state, reason = SparkState.RECOVER, "专用徽章明确标注火花待恢复"
    elif inactive and not active and not recover and not uncertain:
        state, reason = SparkState.INACTIVE, "专用徽章明确标注火花未点亮或已熄灭"
    else:
        state, reason = SparkState.UNKNOWN, "缺少明确火花徽章语义，或徽章状态冲突"
    flame = raw.get("flame")
    if flame is not None:
        flame_state = flame["state"]
        if (
            flame_state in {1, 2}
            and raw.get("flameVisible")
            and not recover
            and not inactive
            and not uncertain
        ):
            state, reason = (
                SparkState.ACTIVE,
                (
                    f"本行专用火花元数据为 {'LIGHT' if flame_state == 1 else 'GRAY'}"
                    "（有效火花），处于有效时间窗且徽章可见"
                ),
            )
        elif flame_state == 3 and not active and not inactive and not uncertain:
            state, reason = (
                SparkState.RECOVER,
                "本行专用火花元数据为 RECOVER（待恢复），处于有效时间窗",
            )
        elif flame_state == 4 and not active and not recover and not uncertain:
            state, reason = (
                SparkState.INACTIVE,
                "本行专用火花元数据为 LIGHT_DOWN（已熄灭）",
            )
        else:
            state, reason = SparkState.UNKNOWN, "火花元数据与可见徽章不一致，需人工确认"
    elif raw.get("flameVisible"):
        state, reason = SparkState.UNKNOWN, "火花图标可见，但缺少有效时间窗内的明确状态元数据"
    if is_group:
        reason += "；群聊不可导入"
    elif conversation_type != "single":
        reason += "；单聊类型待确认"
    if not strong:
        reason += "；身份缺失、冲突或未明确绑定，需人工确认"
    evidence["spark_evidence"] = (
        "explicit_accessible_badge" if state is not SparkState.UNKNOWN else "unresolved"
    )
    if flame is not None and state is not SparkState.UNKNOWN:
        evidence.update(
            spark_evidence="pcim_flame_infos",
            flame_state=flame["state"],
            flame_start=flame["start"],
            flame_end=flame["end"],
        )
    return SparkContact(candidate, state, reason, is_group)


async def scan_contacts(
    adapter: DouyinChatAdapter,
    account: Account,
    *,
    progress: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    max_contacts: int = 1000,
    max_rounds: int = 160,
    timeout_seconds: float = 90,
    load_wait_ms: int = 650,
) -> SparkScanResult:
    contacts: list[SparkContact] = []
    stable: dict[str, int] = {}
    weak_observations: set[tuple] = set()
    scanned_at = datetime.now().astimezone().isoformat(timespec="seconds")
    status = SparkScanStatus.PARTIAL
    detail = "达到扫描轮数上限，未证明列表完整"
    previous = None
    stagnant = 0
    observation = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            if cancel and cancel.is_set():
                return SparkScanResult(
                    account.platform_user_id,
                    account.logged_in_at,
                    scanned_at,
                    SparkScanStatus.CANCELLED,
                    (),
                    0,
                    "扫描已取消。" + RECOGNITION_LIMIT,
                )
            await adapter._require_ready()
            await adapter.page.evaluate(_SNAPSHOT_JS, {"operation": "reset", "limit": max_contacts})
            await adapter.page.wait_for_timeout(load_wait_ms)
            for _ in range(max_rounds):
                if cancel and cancel.is_set():
                    status, detail = SparkScanStatus.CANCELLED, "扫描已取消，保留已扫描结果"
                    break
                await adapter._require_ready()
                snapshot = await adapter.page.evaluate(
                    _SNAPSHOT_JS, {"operation": "read", "limit": max_contacts}
                )
                if not snapshot["found"]:
                    detail = "未找到具有明确语义的唯一会话列表，无法证明扫描范围"
                    break
                before = len(contacts)
                capped = False
                for raw in snapshot["rows"]:
                    observation += 1
                    contact = _contact(raw, observation)
                    strong_identity = contact.candidate.evidence["identity_strength"] == "strong"
                    scan_digest = contact.candidate.evidence.get("scan_conversation_digest")
                    key = (
                        contact.candidate.stable_key
                        if strong_identity
                        else f"conversation:{scan_digest}"
                        if scan_digest
                        else ""
                    )
                    if key:
                        if key in stable:
                            index = stable[key]
                            old = contacts[index]
                            identity_conflict = (
                                old.candidate.evidence.get("identity_conflict")
                                or contact.candidate.evidence.get("identity_conflict")
                                or (
                                    old.candidate.douyin_id
                                    and contact.candidate.douyin_id
                                    and old.candidate.douyin_id != contact.candidate.douyin_id
                                )
                                or old.candidate.evidence["chat_type"]
                                != contact.candidate.evidence["chat_type"]
                            )
                            if identity_conflict or old.spark_state != contact.spark_state:
                                candidate = old.candidate
                                if identity_conflict:
                                    candidate = replace(
                                        candidate,
                                        evidence={**candidate.evidence, "identity_conflict": True},
                                    )
                                contacts[index] = replace(
                                    old,
                                    candidate=candidate,
                                    spark_state=SparkState.UNKNOWN,
                                    reason=(
                                        "同一稳定身份的身份或会话类型观察冲突，不可导入"
                                        if identity_conflict
                                        else "同一稳定身份的火花状态观察冲突，需人工确认"
                                    ),
                                    is_group=old.is_group or contact.is_group,
                                )
                            continue
                    else:
                        # Only repeat observations at the very same scroll position/slot are skipped.
                        marker = (
                            snapshot["top"],
                            raw["slot"],
                            raw["name"],
                            raw["dataId"],
                            tuple(raw["profiles"]),
                            tuple(raw["ids"]),
                            tuple(raw["badges"]),
                            tuple(raw["types"]),
                        )
                        if marker in weak_observations:
                            continue
                        weak_observations.add(marker)
                    if len(contacts) >= max_contacts:
                        capped = True
                        break
                    if key:
                        stable[key] = len(contacts)
                    contacts.append(contact)
                if progress:
                    progress(
                        f"已扫描 {len(contacts)} 项，可导入 {sum(item.importable for item in contacts)} 项"
                    )
                if cancel and cancel.is_set():
                    status, detail = SparkScanStatus.CANCELLED, "扫描已取消，保留已扫描结果"
                    break
                if capped or snapshot["overflow"]:
                    detail = "达到扫描数量上限，未证明列表完整"
                    break
                if snapshot["end"]:
                    status, detail = (
                        SparkScanStatus.COMPLETE,
                        "已观察到当前会话列表的明确末端标识（不代表全部账号联系人）",
                    )
                    break
                signature = (
                    snapshot["top"],
                    tuple(
                        (raw["name"], tuple(raw["profiles"]), tuple(raw["ids"]), raw["dataId"])
                        for raw in snapshot["rows"]
                    ),
                )
                stagnant = stagnant + 1 if signature == previous and len(contacts) == before else 0
                previous = signature
                if stagnant >= 3 and not snapshot["loading"]:
                    detail = "列表滚动停滞且无明确末端标识，仅完成部分扫描"
                    break
                await adapter.page.evaluate(
                    _SNAPSHOT_JS, {"operation": "scroll", "limit": max_contacts}
                )
                await adapter.page.wait_for_timeout(load_wait_ms)
    except TimeoutError:
        detail = "扫描时间耗尽，仅保留已扫描结果"
    except AutomationError as error:
        if error.code in {ErrorCode.AUTHENTICATION_REQUIRED, ErrorCode.HUMAN_VERIFICATION_REQUIRED}:
            raise
        detail = "页面不再就绪，扫描提前停止"
    except PlaywrightError:
        detail = "页面读取或滚动中断，扫描提前停止"
    if cancel and cancel.is_set():
        status, detail = SparkScanStatus.CANCELLED, "扫描已取消，保留已扫描结果"
    return SparkScanResult(
        account.platform_user_id,
        account.logged_in_at,
        scanned_at,
        status,
        tuple(contacts),
        len(contacts),
        detail + "。" + RECOGNITION_LIMIT,
    )
