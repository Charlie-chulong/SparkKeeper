from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from ..logging_safe import safe_url
from ..models import Account, FriendCandidate, Target
from .browser import BrowserSession, BrowserSessionFactory
from .errors import (
    AuthenticationRequired,
    ComposerUnavailable,
    HumanVerificationRequired,
    PageNotReady,
    PageStructureChanged,
    SendFailed,
    SendUnknown,
    TargetAmbiguous,
    TargetIdentityMismatch,
    TargetNotFound,
)
from .send_state import DeliveryOutcome, DeliverySample, await_delivery_terminal

CHAT_URL = "https://www.douyin.com/chat"
LOGIN_TIMEOUT_SECONDS = 300


class PageState(StrEnum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    HUMAN_VERIFICATION = "human_verification"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class LoginResult:
    account: Account


_TRIGGER_CALLBACK = Callable[[], None | Awaitable[None]]
_STATUS_CALLBACK = Callable[[str], None]


_VISIBLE_JS = """element => {
    const r = element.getBoundingClientRect();
    const s = getComputedStyle(element);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
}"""


# Reproduce public PC IM module 2540's participant identity lookup without
# calling its cache-writing SecUserId getter. The scan additionally reads only
# the public streak component's dedicated flame metadata key. No props, full
# coreInfo/ext, participants or messages are serialized.
_CONVERSATION_IDENTITY_JS = r"""(element, includeFlame = false, diagnostic = {}) => {
    const unavailable = reason => {
        diagnostic.reason = reason;
        return null;
    };
    if (!element) return unavailable('element_missing');
    const row = element.closest('.conversationConversationItemwrapper');
    // The live header's memoized component owns curConversation above its
    // title/summary/user DOM wrappers. Stop before the surrounding right pane.
    const chatHeader = row ? null : element.closest('.RightPanelHeaderconvHeader');
    const root = row || chatHeader || element.closest('.RightPanelHeadertitleContainer') || element;
    diagnostic.root_class = typeof root.className === 'string' ? root.className : '';
    const fiberKey = Object.keys(root).find(key => key.startsWith('__reactFiber$'));
    let fiber = fiberKey ? root[fiberKey] : null;
    diagnostic.fiber_present = !!fiber;
    const boundConversation = props => chatHeader
        ? props?.curConversation ?? props?.conversation ?? props?.item?.conversation
        : props?.conversation ?? props?.item?.conversation;
    for (let depth = 0; fiber && depth < 16; depth++, fiber = fiber.return) {
        diagnostic.fiber_depth = depth;
        if (depth && fiber.stateNode instanceof Element
            && fiber.stateNode !== root && !root.contains(fiber.stateNode)) {
            return unavailable('host_boundary');
        }
        const conversation = boundConversation(fiber.memoizedProps);
        if (!conversation || typeof conversation !== 'object') continue;
        diagnostic.binding = chatHeader && fiber.memoizedProps?.curConversation
            ? 'curConversation' : fiber.memoizedProps?.conversation ? 'conversation' : 'item.conversation';
        try {
            // Virtualized React nodes can retain the previous conversation in the
            // alternate tree. A conflicting identity must never be treated as current.
            const alternate = boundConversation(fiber.alternate?.memoizedProps);
            if (alternate && alternate !== conversation
                && (!conversation.id || alternate.id !== conversation.id
                    || alternate.type !== conversation.type)) return unavailable('alternate_conflict');
            const type = conversation.type;
            const participantUserId = type === 1 ? conversation.toParticipantUserId : '';
            const participants = type === 1 ? conversation.firstPageParticipant?.participants : null;
            const matches = participantUserId && Array.isArray(participants) && participants.length <= 2
                ? participants.filter(participant => String(participant?.user_id) === String(participantUserId))
                : [];
            const secUserId = matches.length === 1 ? matches[0].sec_uid : '';
            diagnostic.participant_count = Array.isArray(participants) ? participants.length : null;
            diagnostic.matching_participant_count = matches.length;
            diagnostic.reason = secUserId ? 'participant_identity' : 'participant_identity_missing';
            let flame = null;
            if (includeFlame) {
                try {
                    const encoded = conversation.coreInfo?.ext?.['a:consecutive_chat_data'];
                    if (typeof encoded === 'string' && encoded.length <= 262144) {
                        const infos = JSON.parse(encoded)?.flame_infos;
                        const now = Math.floor(Date.now() / 1000);
                        if (Array.isArray(infos) && infos.length <= 64) {
                            const current = infos.filter(info => Number.isFinite(info?.start)
                                && Number.isFinite(info?.end) && info.start <= now && now <= info.end);
                            if (current.length && current.every(info => info.state === current[0].state)
                                && [1, 2, 3, 4].includes(current[0].state)) {
                                flame = {state: current[0].state, start: current[0].start, end: current[0].end};
                            }
                        }
                    }
                } catch (_) { /* Unavailable/malformed dedicated metadata stays unknown. */ }
            }
            const scanConversationId = includeFlame ? conversation.id : '';
            return {
                secUserId: typeof secUserId === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(secUserId)
                    ? secUserId : '',
                type: typeof type === 'number' ? type : null,
                flame,
                scanConversationId: typeof scanConversationId === 'string' && scanConversationId.length <= 500
                    ? scanConversationId : ''
            };
        } catch (_) { return unavailable('binding_unreadable'); }
    }
    return unavailable(fiber ? 'depth_limit' : 'binding_missing');
}"""


SPARK_STICKER_RESOURCE_PATH = "/obj/im-resource/1687263281313-ts-e7bbade781abe88ab12e706e67"
_SPARK_PANEL_SELECTOR = ".componentsemojiemojiPanel"
_SPARK_ITEM_SELECTOR = ".emojiEmojiItememojiItem"
_SPARK_ENTRY_SELECTOR = (
    '[data-e2e="msg-input"] .messageMsgInputinputAction .messageMsgInputiconAction'
)

# Public PC IM __federation_expose_default_export.0ccd0cf4.js:
# Interactive items send BIG_EMOJI(5), aweType 507 immediately on click.
# Read only the bound conversation and matching native message/item props;
# never serialize unrelated messages, SDK stores, callbacks or credentials.
_SPARK_STICKER_SNAPSHOT_JS = r"""({resourcePath, requirePanel}) => {
    const visible = element => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const resourceMatches = value => {
        if (typeof value !== 'string') return false;
        try { return new URL(value).pathname === resourcePath; }
        catch (_) { return false; }
    };
    const fiberFor = element => {
        const key = Object.keys(element).find(key => key.startsWith('__reactFiber$'));
        return key ? element[key] : null;
    };
    const conversationFor = root => {
        for (let fiber = fiberFor(root), depth = 0; fiber && depth < 16;
            fiber = fiber.return, depth++) {
            if (depth && fiber.stateNode instanceof Element
                && fiber.stateNode !== root && !root.contains(fiber.stateNode)) break;
            const props = fiber.memoizedProps;
            const conversation = props?.curConversation ?? props?.conversation;
            if (!conversation) continue;
            const alternate = fiber.alternate?.memoizedProps;
            const other = alternate?.curConversation ?? alternate?.conversation;
            if (other && (other.id !== conversation.id || other.type !== conversation.type)) {
                return null;
            }
            return conversation;
        }
        return null;
    };
    const boundProp = (root, name) => {
        for (let fiber = fiberFor(root), depth = 0; fiber && depth < 16;
            fiber = fiber.return, depth++) {
            if (depth && fiber.stateNode instanceof Element
                && fiber.stateNode !== root && !root.contains(fiber.stateNode)) break;
            const value = fiber.memoizedProps?.[name];
            if (!value) continue;
            const other = fiber.alternate?.memoizedProps?.[name];
            if (name === 'message' && other && (other.clientId !== value.clientId
                || other.conversationId !== value.conversationId)) return null;
            return value;
        }
        return null;
    };
    const headers = [...document.querySelectorAll('.RightPanelHeaderconvHeader')].filter(visible);
    if (headers.length !== 1) return {error: 'chat_header_unavailable'};
    const conversation = conversationFor(headers[0]);
    if (!conversation || conversation.type !== 1 || typeof conversation.id !== 'string'
        || !conversation.id) return {error: 'chat_binding_unavailable'};
    const decimalIndex = value => {
        if (value == null) return null;
        const text = String(value);
        return /^\d{1,30}$/.test(text) ? text : null;
    };
    // Both fields are the SDK's monotonic V2 watermarks, not read indexes.
    // Long values stay decimal strings; Number would lose 64-bit precision.
    const indexes = [conversation.lastMessageIndexV2, conversation.maxIndexV2FromServer]
        .map(decimalIndex).filter(value => value !== null);
    const watermark = indexes.reduce((max, value) =>
        BigInt(value) > BigInt(max) ? value : max, '0');
    if (requirePanel && BigInt(watermark) <= 0n) {
        return {error: 'conversation_watermark_unavailable'};
    }
    if (requirePanel) {
        const panels = [...document.querySelectorAll('.componentsemojiemojiPanel')].filter(visible);
        if (panels.length !== 1) return {error: 'native_panel_unavailable'};
        const panel = panels[0];
        // The portal's Semi modal hosts sit between the panel and its owner.
        const modal = panel.closest('.componentsemojiim-saas-modal');
        const panelConversation = modal ? conversationFor(modal) : null;
        if (!panelConversation || panelConversation.id !== conversation.id) {
            return {error: 'native_panel_wrong_conversation'};
        }
        const items = [...panel.querySelectorAll('.emojiEmojiItememojiItem')].filter(item => {
            const label = item.querySelector('.emojiEmojiItememojiItemDesc');
            return visible(item) && label?.textContent?.trim() === '续火花';
        });
        if (items.length !== 1) return {error: 'native_item_not_unique'};
        const item = items[0];
        const sticker = boundProp(item, 'sticker');
        const images = [...item.querySelectorAll('.emojiEmojiItemimgBox img')].filter(visible);
        if (!sticker || sticker.display_name !== '续火花' || (sticker.id ?? 0) !== 0
            || sticker.resource_type !== 4 || sticker.animate_type !== 'png'
            || !resourceMatches(sticker.static_url) || !resourceMatches(sticker.animate_url)
            || images.length !== 1 || !images[0].complete || images[0].naturalWidth <= 0
            || !resourceMatches(images[0].currentSrc || images[0].src)) {
            return {error: 'native_item_identity_unavailable'};
        }
    }
    const messages = [];
    const ids = new Set();
    for (const box of document.querySelectorAll('.MessageItemEmojiemojiBox')) {
        if (!visible(box) || ![...box.querySelectorAll('img')].some(image =>
            resourceMatches(image.currentSrc || image.src))) continue;
        const message = boundProp(box, 'message');
        if (!message) return {error: 'native_message_binding_unavailable'};
        if (message.isFromMe !== true || message.type !== 5 || message.isRefMessage
            || message.isRecalled || message.visible !== true
            || message.conversationId !== conversation.id) continue;
        const content = message.parsedContent;
        if (content?.display_name !== '续火花' || content.image_id !== 0
            || content.aweType !== 507 || content.resource_type !== 4
            || !resourceMatches(content.url?.uri)
            || !content.url?.url_list?.some(resourceMatches)) continue;
        const clientId = message.clientId;
        if (typeof clientId !== 'string' || !clientId) {
            return {error: 'native_message_id_unavailable'};
        }
        if (ids.has(clientId)) continue;
        ids.add(clientId);
        const serverId = typeof message.serverId === 'string' ? message.serverId : '';
        const indexV2 = decimalIndex(message.indexInConversationV2);
        const flightStatus = message.flightStatus;
        // Public legacy SDK Message.fromServerMessage does not initialize
        // flightStatus. Only its online, enabled server representation can
        // supply this alternative proof; null/unknown/pending states cannot.
        const serverHydrated = flightStatus === undefined && message.isOffline === false
            && message.serverStatus === 0 && /^\d{1,30}$/.test(serverId) && BigInt(serverId) > 0n
            && indexV2 !== null && BigInt(indexV2) > 0n;
        messages.push({
            clientId,
            serverId,
            flightStatus,
            serverHydrated,
            indexV2
        });
    }
    return {conversationId: conversation.id, watermark, messages};
}"""


class DouyinChatAdapter:
    """只通过当前可见网页执行正常扫码、搜索、输入和发送。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self._last_candidate_diagnostics: dict[str, Any] = {}
        self._last_chat_diagnostics: dict[str, Any] = {}

    async def classify_page(self) -> PageState:
        state = await self.page.evaluate(
            r"""() => {
                const visible = element => {
                    const r = element.getBoundingClientRect();
                    const s = getComputedStyle(element);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                const textExists = phrases => [...document.querySelectorAll('body *')].some(element => {
                    if (!visible(element) || element.children.length > 3) return false;
                    const text = (element.innerText || '').replace(/\s+/g, ' ').trim();
                    return phrases.some(phrase => text === phrase || text.includes(phrase));
                });
                const loginInput = [...document.querySelectorAll('input')].some(element =>
                    visible(element) && ['请输入手机号', '请输入验证码'].includes(element.placeholder)
                );
                const loginPanel = loginInput && textExists(['扫码登录', '密码登录']);
                if (!loginPanel && textExists([
                    '请完成安全验证', '安全验证', '访问过于频繁', '环境异常',
                    '账号存在风险', '请完成下方验证', '拖动滑块完成拼图'
                ])) return 'human_verification';
                if (loginPanel) return 'login_required';

                const chatSearch = [...document.querySelectorAll('input')].some(element => {
                    if (!visible(element)) return false;
                    const placeholder = element.placeholder || '';
                    return placeholder.includes('搜索') && element.getAttribute('data-e2e') !== 'searchbar-input';
                });
                const composer = [...document.querySelectorAll('[contenteditable="true"], textarea')].some(element => {
                    if (!visible(element)) return false;
                    const r = element.getBoundingClientRect();
                    return r.top > window.innerHeight * 0.45;
                });
                if (chatSearch || composer) return 'ready';
                return 'not_ready';
            }"""
        )
        return PageState(str(state))

    async def open_chat(self) -> None:
        await self.page.goto(CHAT_URL, wait_until="domcontentloaded")
        for attempt in range(2):
            state = await self._wait_for_page_state(15)
            if state is PageState.READY:
                return
            if state is PageState.LOGIN_REQUIRED:
                raise AuthenticationRequired()
            if state is PageState.HUMAN_VERIFICATION:
                raise HumanVerificationRequired()
            if attempt == 0:
                await self.page.reload(wait_until="domcontentloaded")
        diagnostic = await self.safe_diagnostic()
        if diagnostic["chat_search_count"] == 0 and diagnostic["composer_count"] == 0:
            raise PageNotReady()
        raise PageStructureChanged()

    async def wait_for_interactive_login(self, status: _STATUS_CALLBACK | None = None) -> None:
        await self.page.goto(CHAT_URL, wait_until="domcontentloaded")
        loop = __import__("asyncio").get_running_loop()
        deadline = loop.time() + LOGIN_TIMEOUT_SECONDS
        last_state: PageState | None = None
        while loop.time() < deadline:
            if self.page.is_closed():
                raise AuthenticationRequired()
            current = await self.classify_page()
            if current is PageState.READY:
                if status:
                    status("登录成功，正在安全保存登录状态")
                return
            if status and current is not last_state:
                if current is PageState.HUMAN_VERIFICATION:
                    status("请在打开的浏览器中完成人工验证")
                else:
                    status("请在打开的浏览器中使用抖音 App 扫码登录")
            last_state = current
            await self.page.wait_for_timeout(500)
        raise AuthenticationRequired()

    async def _wait_for_page_state(self, timeout_seconds: float) -> PageState:
        loop = __import__("asyncio").get_running_loop()
        deadline = loop.time() + timeout_seconds
        last = PageState.NOT_READY
        while loop.time() < deadline:
            last = await self.classify_page()
            if last is not PageState.NOT_READY:
                return last
            await self.page.wait_for_timeout(300)
        return last

    async def safe_diagnostic(self) -> dict[str, Any]:
        snapshot = await self.page.evaluate(
            r"""() => {
                const visible = element => {
                    const r = element.getBoundingClientRect();
                    const s = getComputedStyle(element);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                const safe = element => ({
                    tag: element.tagName.toLowerCase(),
                    type: element.getAttribute('type'),
                    role: element.getAttribute('role'),
                    placeholder: element.getAttribute('placeholder'),
                    ariaLabel: element.getAttribute('aria-label'),
                    dataE2e: element.getAttribute('data-e2e')
                });
                const inputs = [...document.querySelectorAll('input, textarea')].filter(visible).map(safe);
                return {
                    inputs,
                    chatSearchCount: inputs.filter(item =>
                        (item.placeholder || '').includes('搜索') && item.dataE2e !== 'searchbar-input'
                    ).length,
                    composerCount: [...document.querySelectorAll('[contenteditable="true"], textarea')].filter(visible).length,
                    roleTextboxCount: [...document.querySelectorAll('[role="textbox"]')].filter(visible).length
                };
            }"""
        )
        return {
            "url": safe_url(self.page.url),
            "title": (await self.page.title())[:120],
            "inputs": list(snapshot.get("inputs", []))[:20],
            "chat_search_count": int(snapshot.get("chatSearchCount", 0)),
            "composer_count": int(snapshot.get("composerCount", 0)),
            "role_textbox_count": int(snapshot.get("roleTextboxCount", 0)),
        }

    async def search_targets(self, query: str) -> list[FriendCandidate]:
        query = " ".join(query.split()).strip()
        if not query:
            raise ValueError("搜索关键词不能为空")
        await self._require_ready()
        search = await self._chat_search_input()
        await self._enter_chat_search_query(search, query)
        raw_candidates = await self._wait_for_candidate_rows(query, search)
        candidates: list[FriendCandidate] = []
        seen: set[str] = set()
        for raw in raw_candidates:
            candidate = self._candidate_from_raw(raw)
            if candidate.stable_key in seen:
                continue
            seen.add(candidate.stable_key)
            candidates.append(candidate)
        return candidates

    async def capture_current_chat_candidate(self, expected_name: str) -> FriendCandidate:
        """读取用户已手动打开的当前聊天；不输入文本，也不触发发送。"""

        return await self._current_chat_candidate(expected_name)

    async def _current_chat_candidate(self, expected_name: str) -> FriendCandidate:
        expected_name = self._normalize_text(expected_name)
        if not expected_name:
            raise ValueError("请先填写好友在聊天标题中显示的准确名称")
        self._last_chat_diagnostics = {}
        await self._require_ready()
        composer = await self._composer()
        raw = await composer.evaluate(
            r"""(editor, expected) => {
                const normalize = value => String(value || '')
                    .normalize('NFKC')
                    .replace(/[\s\u200B-\u200D\uFEFF]+/g, ' ')
                    .trim();
                const expectedName = normalize(expected);
                const visible = element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const editorRect = editor.getBoundingClientRect();
                const viewportWidth = window.innerWidth;
                const viewportHeight = window.innerHeight;

                // 从已由 Python 语义定位器确认的编辑器向上找聊天面板。不能再按
                // “最靠右的 contenteditable”重新猜编辑器，否则页面辅助控件会
                // 把标题范围错误地压到右侧。
                let pane = null;
                for (let node = editor.parentElement; node && node !== document.body;
                     node = node.parentElement) {
                    if (!visible(node)) continue;
                    const rect = node.getBoundingClientRect();
                    if (rect.width >= viewportWidth * 0.36
                        && rect.height >= viewportHeight * 0.50
                        && rect.left >= viewportWidth * 0.10
                        && rect.right > editorRect.right - 8) {
                        pane = node;
                        break;
                    }
                }
                const paneRect = pane ? pane.getBoundingClientRect() : {
                    left: Math.max(
                        viewportWidth * 0.25,
                        editorRect.left - Math.max(260, editorRect.width * 1.5)
                    ),
                    right: Math.min(
                        viewportWidth,
                        editorRect.right + Math.max(160, editorRect.width * 0.5)
                    ),
                    top: 0,
                    bottom: viewportHeight,
                    width: viewportWidth * 0.75,
                    height: viewportHeight
                };
                const headerBottom = Math.min(
                    editorRect.top,
                    paneRect.top + Math.max(180, Math.min(320, paneRect.height * 0.42))
                );
                const valuesFor = element => {
                    const values = [];
                    const innerText = element.innerText || '';
                    values.push(...innerText.split(/\n+/).map(normalize));
                    values.push(normalize(element.textContent));
                    values.push(normalize(element.getAttribute('title')));
                    values.push(normalize(element.getAttribute('aria-label')));
                    return [...new Set(values.filter(Boolean))];
                };
                const commonAncestor = (left, right) => {
                    const ancestors = new Set();
                    for (let node = left; node; node = node.parentElement) ancestors.add(node);
                    for (let node = right; node; node = node.parentElement) {
                        if (ancestors.has(node)) return node;
                    }
                    return null;
                };
                const headerMarker = (element, stop) => {
                    for (let node = element; node && node !== stop; node = node.parentElement) {
                        const className = typeof node.className === 'string' ? node.className : '';
                        if (node.tagName === 'HEADER'
                            || /right.*panel.*header|chat.*header/i.test(className)) {
                            return node;
                        }
                    }
                    return null;
                };
                const exactElements = [...document.querySelectorAll(
                    'h1,h2,h3,[role="heading"],[title],[aria-label],a,span,p,strong,div'
                )].filter(element =>
                    visible(element)
                    && element !== editor
                    && !editor.contains(element)
                    && valuesFor(element).includes(expectedName)
                );
                const candidates = [];
                for (const element of exactElements) {
                    const rect = element.getBoundingClientRect();
                    if (rect.bottom > editorRect.top || rect.height > 220) continue;
                    const centerX = rect.left + rect.width / 2;
                    const common = commonAncestor(element, editor);
                    const commonRect = common ? common.getBoundingClientRect() : null;
                    const marker = headerMarker(element, common);
                    const markerRect = marker ? marker.getBoundingClientRect() : null;
                    const insideInitialPane = centerX >= paneRect.left
                        && centerX <= paneRect.right
                        && rect.top >= Math.max(16, paneRect.top)
                        && rect.bottom <= headerBottom;
                    const sharesBoundedPane = common
                        && common !== document.body
                        && common !== document.documentElement
                        && commonRect.width <= viewportWidth * 0.92
                        && commonRect.height >= viewportHeight * 0.40
                        && commonRect.left >= viewportWidth * 0.03
                        && centerX >= Math.max(viewportWidth * 0.25, commonRect.left)
                        && centerX <= commonRect.right
                        && rect.top - commonRect.top
                            <= Math.max(200, Math.min(360, commonRect.height * 0.48));
                    const hasHeaderMarker = marker
                        && visible(marker)
                        && markerRect.bottom <= editorRect.top
                        && markerRect.left + markerRect.width / 2 > viewportWidth * 0.25;
                    if (!insideInitialPane && !sharesBoundedPane && !hasHeaderMarker) continue;

                    const fullText = normalize(element.innerText);
                    const semantic = element.matches('h1,h2,h3,[role="heading"]') ? 80 : 0;
                    const exact = fullText === expectedName ? 60 : 0;
                    const identity = element.matches('a[href*="/user/"]')
                        || element.querySelector('a[href*="/user/"]') ? 40 : 0;
                    const leaf = element.children.length === 0 ? 20 : 0;
                    const relation = hasHeaderMarker ? 120 : sharesBoundedPane ? 80 : 40;
                    candidates.push({
                        element,
                        common,
                        marker,
                        score: semantic + exact + identity + leaf + relation
                            - Math.min(rect.height, 160) / 10
                    });
                }
                if (!candidates.length) {
                    return {
                        error: 'title_missing',
                        exactVisibleCount: exactElements.length
                    };
                }
                candidates.sort((left, right) => right.score - left.score);
                const selected = candidates[0];
                const title = selected.element;
                const activePane = selected.common
                    && selected.common !== document.body
                    && selected.common !== document.documentElement
                    ? selected.common
                    : pane;
                const activePaneRect = activePane ? activePane.getBoundingClientRect() : paneRect;
                const titleRect = title.getBoundingClientRect();
                const activeHeaderBottom = Math.min(
                    editorRect.top,
                    Math.max(
                        titleRect.bottom + 1,
                        activePaneRect.top
                            + Math.max(220, Math.min(380, activePaneRect.height * 0.48))
                    )
                );

                // 扩展到最小标题容器以提取主页、头像和会话标识；共同祖先关系
                // 将标题约束在编辑器所属聊天面板，排除左侧同名会话和正文。
                let header = selected.marker || title;
                for (let depth = 0; depth < 7 && header.parentElement; depth++) {
                    const parent = header.parentElement;
                    if (activePane && parent === activePane) break;
                    if (!visible(parent)) break;
                    const rect = parent.getBoundingClientRect();
                    if (rect.left < activePaneRect.left - 8
                        || rect.right > activePaneRect.right + 8) break;
                    if (rect.top < activePaneRect.top - 8
                        || rect.bottom > activeHeaderBottom
                        || rect.height > 260) break;
                    header = parent;
                }
                const headerText = normalize(header.innerText);
                const escapedName = expectedName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                const expectedWithGroupCount = new RegExp(
                    `(?:^|\\s)${escapedName}\\s*[（(]\\s*\\d+\\s*[）)](?:\\s|$)`
                );
                const selectedTitleHasGroupCount =
                    /[（(]\s*\d+\s*[）)]$/.test(normalize(selected.element.innerText));
                if (/群聊/.test(headerText)
                    || /(?:^|\s)\d+\s*人(?:\s|$)/.test(headerText)
                    || selectedTitleHasGroupCount
                    || expectedWithGroupCount.test(headerText)) {
                    return {error: 'group_chat'};
                }
                const nodes = [header, ...header.querySelectorAll('*')];
                const links = nodes
                    .filter(node => node.matches && node.matches('a[href]'))
                    .map(node => node.href)
                    .filter(Boolean);
                const identityDiagnostic = {};
                const conversationIdentity = (__READ_CONVERSATION_IDENTITY__)(
                    selected.element, false, identityDiagnostic
                );
                const diagnostic = {
                    title_class: typeof title.className === 'string' ? title.className : '',
                    header_class: typeof header.className === 'string' ? header.className : '',
                    conversation: identityDiagnostic
                };
                if (identityDiagnostic.reason === 'alternate_conflict') {
                    return {error: 'identity_conflict', diagnostic};
                }
                if (conversationIdentity && conversationIdentity.type !== null
                    && conversationIdentity.type !== 1) return {error: 'group_chat', diagnostic};
                const participantProfile = conversationIdentity?.secUserId
                    ? `https://www.douyin.com/user/${conversationIdentity.secUserId}` : '';
                const linkedProfile = links.find(href => href.includes('/user/')) || '';
                if (participantProfile && linkedProfile
                    && new URL(linkedProfile).pathname.replace(/\/$/, '')
                        !== new URL(participantProfile).pathname) return {error: 'identity_conflict', diagnostic};
                const profileUrl = participantProfile || linkedProfile;
                const idMatch = headerText.match(/抖音号\s*[:：]\s*([^\s]+)/);
                const dataIds = [];
                for (const node of nodes) {
                    for (const attribute of node.attributes || []) {
                        if (/(uid|user.?id|conversation.?id)/i.test(attribute.name)
                            && attribute.value && attribute.value.length <= 200) {
                            dataIds.push(`${attribute.name}:${attribute.value}`);
                        }
                    }
                }
                const image = nodes.find(node =>
                    node.matches && node.matches('img') && visible(node)
                );
                return {
                    diagnostic,
                    token: '',
                    name: expectedName,
                    douyinId: idMatch ? idMatch[1] : '',
                    profileUrl,
                    avatarUrl: image ? (image.currentSrc || image.src || '') : '',
                    dataId: [...new Set(dataIds)].sort().join('|'),
                    lineCount: headerText ? headerText.split(/\n+/).length : 1,
                    source: 'current_chat'
                };
            }""".replace("__READ_CONVERSATION_IDENTITY__", _CONVERSATION_IDENTITY_JS),
            expected_name,
        )
        self._last_chat_diagnostics = raw.get("diagnostic") or {
            "reason": raw.get("error") or "diagnostic_unavailable",
            "exact_visible_count": raw.get("exactVisibleCount"),
        }
        error = str(raw.get("error") or "")
        if error == "group_chat":
            raise TargetAmbiguous()
        if error == "title_missing":
            if int(raw.get("exactVisibleCount") or 0):
                raise TargetIdentityMismatch("页面中存在同名文字，但无法确认它是右侧当前聊天标题")
            raise TargetIdentityMismatch(
                "右侧聊天顶部未显示与输入完全相同的名称；有备注时填写顶部备注名，否则填写昵称"
            )
        if error or not raw.get("name"):
            raise TargetIdentityMismatch()
        candidate = self._candidate_from_raw(raw)
        return FriendCandidate(
            stable_key=candidate.stable_key,
            display_name=candidate.display_name,
            douyin_id=candidate.douyin_id,
            profile_url=candidate.profile_url,
            avatar_url=candidate.avatar_url,
            evidence={**candidate.evidence, "capture_source": "current_chat"},
        )

    async def open_confirmed_target(self, target: Target) -> None:
        await self._require_ready()
        search = await self._chat_search_input()
        await self._enter_chat_search_query(
            search,
            target.search_query or target.display_name,
        )
        raw_candidates = await self._wait_for_candidate_rows(
            target.search_query or target.display_name,
            search,
        )
        matches: list[dict[str, Any]] = []
        for raw in raw_candidates:
            candidate = self._candidate_from_raw(raw)
            if self._identity_matches(candidate, target):
                matches.append(raw)

        def lookup_error(reason: str) -> TargetNotFound | TargetAmbiguous:
            error = TargetAmbiguous() if reason == "ambiguous" else TargetNotFound()
            diagnostic = {
                "reason": reason,
                "target": {
                    "id": target.id,
                    "display_name": target.display_name,
                    "search_query": target.search_query or target.display_name,
                    "stable_key": target.stable_key,
                    "douyin_id": target.douyin_id,
                    "profile_url": target.profile_url,
                    "avatar_url": target.avatar_url,
                    "identity_strength": target.evidence.get("identity_strength"),
                    "data_id_digest": target.evidence.get("data_id_digest"),
                    "capture_source": target.evidence.get("capture_source"),
                },
                "candidate_count": len(raw_candidates),
                "match_count": len(matches),
                "candidates": [
                    {
                        "token": raw.get("token"),
                        "display_name": candidate.display_name,
                        "stable_key": candidate.stable_key,
                        "douyin_id": candidate.douyin_id,
                        "profile_url": candidate.profile_url,
                        "avatar_url": candidate.avatar_url,
                        "data_id": raw.get("dataId"),
                        "identity_strength": candidate.evidence["identity_strength"],
                        "matches_saved_identity": self._identity_matches(candidate, target),
                    }
                    for raw in raw_candidates
                    for candidate in [self._candidate_from_raw(raw)]
                ],
                "page_url": self.page.url,
                "page_state_before_search": PageState.READY.value,
                "extraction": self._last_candidate_diagnostics,
            }
            error.add_note(json.dumps(diagnostic, ensure_ascii=False))
            return error

        if not matches:
            raise lookup_error("identity_mismatch" if raw_candidates else "no_candidates")
        if len(matches) > 1:
            raise lookup_error("ambiguous")
        token = str(matches[0]["token"])
        row = self.page.locator(f'[data-sk-candidate-token="{token}"]').first
        if not await row.count():
            raise lookup_error("row_missing")
        if not await row.is_visible():
            raise lookup_error("row_not_visible")
        message_button = row.get_by_role("button", name=re.compile("发消息|聊天"))
        if await message_button.count() and await message_button.first.is_visible():
            await message_button.first.click()
        else:
            text_actions = row.get_by_text(re.compile(r"^\s*(?:发消息|聊天)\s*$"))
            clicked = False
            for index in range(await text_actions.count()):
                action = text_actions.nth(index)
                if await action.is_visible():
                    await action.click()
                    clicked = True
                    break
            if not clicked:
                await row.click()
        await self._wait_for_composer()
        await self.verify_recipient(target)

    async def verify_recipient(self, target: Target) -> None:
        candidate = None
        try:
            candidate = await self._current_chat_candidate(target.display_name)
            target_is_strong = target.evidence.get("identity_strength") == "strong"
            candidate_is_strong = candidate.evidence.get("identity_strength") == "strong"
            if target.evidence.get("capture_source") == "spark_scan" and not candidate_is_strong:
                raise TargetIdentityMismatch(
                    "扫描导入的好友需要稳定身份复核，当前聊天身份不足，请重新确认"
                )
            if (
                target_is_strong
                and candidate_is_strong
                and not self._identity_matches(candidate, target)
            ):
                raise TargetIdentityMismatch()
        except TargetIdentityMismatch as error:
            error.add_note(json.dumps({
                "stage": "current_chat_identity",
                "target_id": target.id,
                "expected_profile": target.profile_url,
                "current_profile": candidate.profile_url if candidate else "",
                "identity_strength": candidate.evidence.get("identity_strength") if candidate else None,
                "extraction": self._last_chat_diagnostics,
            }, ensure_ascii=False))
            raise

    async def composer_available(self) -> bool:
        try:
            await self._composer()
            return True
        except ComposerUnavailable:
            return False

    async def send_text_and_confirm(self, text: str, on_trigger: _TRIGGER_CALLBACK) -> None:
        if not text.strip():
            raise ComposerUnavailable()
        composer = await self._composer()
        before_count = await self._matching_outgoing_count(text)
        try:
            await composer.click()
            await composer.fill(text)
        except Exception as exc:
            raise ComposerUnavailable() from exc
        current = await composer.evaluate(
            "element => 'value' in element ? element.value : element.innerText"
        )
        if self._normalize_text(str(current)) != self._normalize_text(text):
            raise ComposerUnavailable()

        callback_result = on_trigger()
        if inspect.isawaitable(callback_result):
            await callback_result
        try:
            send_button = self.page.get_by_role("button", name="发送", exact=True)
            visible_button = None
            for index in range(await send_button.count()):
                candidate = send_button.nth(index)
                if await candidate.is_visible():
                    visible_button = candidate
                    break
            if visible_button is not None:
                await visible_button.click()
            else:
                await composer.press("Enter")
        except Exception as exc:
            raise SendUnknown() from exc

        async def sample() -> DeliverySample:
            raw = await self._sample_delivery(text, before_count)
            return DeliverySample(
                new_matching_outgoing=bool(raw["newMatchingOutgoing"]),
                pending=bool(raw["pending"]),
                failed=bool(raw["failed"]),
            )

        outcome = await await_delivery_terminal(sample)
        if outcome is DeliveryOutcome.FAILED:
            raise SendFailed()
        if outcome is DeliveryOutcome.UNKNOWN:
            raise SendUnknown()

    async def _spark_sticker_snapshot(self, *, require_panel: bool = False) -> dict[str, Any]:
        snapshot = await self.page.evaluate(
            _SPARK_STICKER_SNAPSHOT_JS,
            {"resourcePath": SPARK_STICKER_RESOURCE_PATH, "requirePanel": require_panel},
        )
        if not isinstance(snapshot, dict) or snapshot.get("error"):
            raise PageStructureChanged()
        return snapshot

    async def _prepare_spark_sticker(self, target: Target) -> tuple[Locator, dict[str, Any]]:
        await self.verify_recipient(target)
        panels = self.page.locator(f"{_SPARK_PANEL_SELECTOR}:visible")
        if await panels.count() == 0:
            entry = self.page.locator(f"{_SPARK_ENTRY_SELECTOR}:visible")
            if await entry.count() != 1:
                raise PageStructureChanged()
            # This handler only sets emojiModalVisible=true; it does not send.
            await entry.click()
        await panels.wait_for(state="visible")
        item = panels.locator(_SPARK_ITEM_SELECTOR).filter(
            has=self.page.locator(".emojiEmojiItememojiItemDesc").filter(
                has_text=re.compile(r"^续火花$")
            )
        )
        if await item.count() != 1 or not await item.is_visible():
            raise PageStructureChanged()
        image = item.locator(".emojiEmojiItemimgBox img")
        if await image.count() != 1:
            raise PageStructureChanged()
        await self.page.wait_for_function(
            "image => image.complete && image.naturalWidth > 0",
            arg=await image.element_handle(),
        )
        action = item.locator('.emojiEmojiItemimgBox[data-apm-action="EmojiItem"]')
        if await action.count() != 1 or not await action.is_visible():
            raise PageStructureChanged()
        # Opening/loading the panel can await. Recheck identity at the final
        # preparation boundary, then reuse this same complete snapshot as baseline.
        await self.verify_recipient(target)
        baseline = await self._spark_sticker_snapshot(require_panel=True)
        return action, baseline

    async def validate_spark_sticker(self, target: Target) -> None:
        """只打开并核验原生面板，绝不选择表情、编辑正文或触发发送。"""
        await self._prepare_spark_sticker(target)

    async def send_spark_sticker_and_confirm(
        self, target: Target, on_trigger: _TRIGGER_CALLBACK
    ) -> None:
        item, baseline = await self._prepare_spark_sticker(target)
        previous_ids = {message["clientId"] for message in baseline["messages"]}
        conversation_id = baseline["conversationId"]
        watermark = int(baseline["watermark"])
        tracked_id: str | None = None
        ambiguous = False
        action = await item.element_handle()
        if action is None:
            raise PageStructureChanged()
        try:
            callback_result = on_trigger()
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception:
            await action.dispose()
            raise
        try:
            await self.verify_recipient(target)
            ready = await self._spark_sticker_snapshot(require_panel=True)
            if ready["conversationId"] != conversation_id:
                raise SendUnknown()
            previous_ids.update(message["clientId"] for message in ready["messages"])
            watermark = max(watermark, int(ready["watermark"]))
            # The item itself is the irreversible action: one click, no Enter,
            # no second send button and no retry after an uncertain result.
            await action.click()

            async def sample() -> DeliverySample:
                nonlocal tracked_id, ambiguous
                snapshot = await self._spark_sticker_snapshot()
                if snapshot["conversationId"] != conversation_id:
                    raise SendUnknown()
                messages = [
                    message for message in snapshot["messages"]
                    if message["clientId"] not in previous_ids
                ]
                # A higher SDK sequence also proves freshness when a fast send
                # completes before the first poll. Never use local wall-clock
                # time: SDK creation times include a server clock offset.
                def newer(message: dict[str, Any]) -> bool:
                    value = message["indexV2"]
                    return isinstance(value, str) and value.isdecimal() and int(value) > watermark

                if tracked_id is None:
                    candidates = [message for message in messages if newer(message)]
                    if len(candidates) > 1:
                        ambiguous = True
                    elif len(candidates) == 1:
                        tracked_id = candidates[0]["clientId"]
                matches = [message for message in messages if message["clientId"] == tracked_id]
                if ambiguous or len(matches) != 1:
                    return DeliverySample(False, False, False)
                message = matches[0]
                status = message["flightStatus"]
                # SDK Received(4) is the online self-message acknowledgement,
                # not a pending state. Server-hydrated messages omit flightStatus.
                # Neither terminal shape bypasses the per-click V2/client guards.
                terminal = status in (3, 4) or (status is None and message["serverHydrated"])
                succeeded = terminal and newer(message) and message["serverId"] not in ("", "0")
                return DeliverySample(
                    new_matching_outgoing=True,
                    pending=not succeeded,
                    failed=status in (-1, -2),
                )

            outcome = await await_delivery_terminal(sample)
        except Exception as exc:
            raise SendUnknown() from exc
        finally:
            await action.dispose()
        if outcome is DeliveryOutcome.FAILED:
            raise SendFailed()
        if outcome is DeliveryOutcome.UNKNOWN:
            raise SendUnknown()

    async def _require_ready(self) -> None:
        state = await self.classify_page()
        if state is PageState.LOGIN_REQUIRED:
            raise AuthenticationRequired()
        if state is PageState.HUMAN_VERIFICATION:
            raise HumanVerificationRequired()
        if state is not PageState.READY:
            raise PageNotReady()

    async def _chat_search_input(self) -> Locator:
        inputs = self.page.locator("input")
        for index in range(await inputs.count()):
            candidate = inputs.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                placeholder = (await candidate.get_attribute("placeholder")) or ""
                data_e2e = (await candidate.get_attribute("data-e2e")) or ""
                if "搜索" in placeholder and data_e2e != "searchbar-input":
                    return candidate
            except PlaywrightError:
                # 页面重绘会使单个 locator 失效；继续检查其余候选。
                continue
        raise PageStructureChanged()

    @staticmethod
    async def _enter_chat_search_query(search: Locator, query: str) -> None:
        # Unicode insertion lacks a complete key cycle in Chromium. Finish with
        # a normal keyboard edit, preserving the query and never pressing Enter.
        await search.click()
        await search.press("Control+A")
        await search.press("Backspace")
        await search.press_sequentially(query, delay=100)
        await search.press("Space")
        await search.press("Backspace")

    async def _wait_for_candidate_rows(
        self,
        query: str,
        search: Locator,
    ) -> list[dict[str, Any]]:
        # A fresh live search can leave the first submitted query empty. Only
        # after a full empty window, recheck readiness and resubmit once using
        # the current input. Never retry a nonempty result or any click/send.
        self._last_candidate_diagnostics = {}
        previous_search_attempts: list[dict[str, Any]] = []
        for submission in range(2):
            if submission:
                await self._require_ready()
                search = await self._chat_search_input()
                await self._enter_chat_search_query(search, query)
            for attempt in range(20):
                await self.page.wait_for_timeout(250)
                rows = await self._extract_candidate_rows(query, search)
                self._last_candidate_diagnostics.update(
                    poll_count=attempt + 1,
                    poll_wait_ms=(attempt + 1) * 250,
                    query_submissions=submission + 1,
                    total_poll_count=submission * 20 + attempt + 1,
                    total_poll_wait_ms=(submission * 20 + attempt + 1) * 250,
                    previous_search_attempts=list(previous_search_attempts),
                )
                if rows:
                    return rows
            if not submission:
                previous_search_attempts.append(self._last_candidate_diagnostics.copy())
        return []

    async def _wait_for_composer(self) -> Locator:
        for _ in range(40):
            try:
                return await self._composer()
            except ComposerUnavailable:
                await self.page.wait_for_timeout(250)
        raise ComposerUnavailable()

    async def _composer(self) -> Locator:
        candidates = self.page.locator('[contenteditable="true"], textarea')
        best: tuple[float, Locator] | None = None
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
                if not box or box["y"] < 400:
                    continue
                score = float(box["y"] * 10 + box["x"])
                if best is None or score > best[0]:
                    best = (score, candidate)
            except PlaywrightError:
                # 页面重绘会使单个 locator 失效；继续检查其余候选。
                continue
        if best is None:
            raise ComposerUnavailable()
        return best[1]

    async def _extract_candidate_rows(
        self,
        query: str,
        search: Locator,
    ) -> list[dict[str, Any]]:
        snapshot = await search.evaluate(
            r"""(searchInput, query) => {
                const normalize = value => String(value || '')
                    .normalize('NFKC')
                    .replace(/[\s\u200B-\u200D\uFEFF]+/g, ' ')
                    .trim();
                const needle = normalize(query);
                const visible = element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const searchRect = searchInput.getBoundingClientRect();
                const viewportWidth = window.innerWidth;
                const viewportHeight = window.innerHeight;
                const bounds = rect => ({
                    left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
                    width: rect.width, height: rect.height
                });
                const leafCounts = {
                    examined: 0, not_visible: 0, too_many_children: 0, query_mismatch: 0,
                    descendant_match: 0, text_too_long: 0, outside_panel: 0, accepted: 0
                };
                const rowCounts = {
                    examined: 0, not_visible: 0, outside_panel: 0, invalid_shape: 0,
                    text_too_long: 0, missing_avatar_or_action: 0, no_row: 0,
                    duplicate_row: 0, group_text: 0, non_private_conversation: 0,
                    conflicting_profile: 0
                };
                let panel = null;
                for (let node = searchInput.parentElement;
                     node && node !== document.body;
                     node = node.parentElement) {
                    if (!visible(node)) continue;
                    const rect = node.getBoundingClientRect();
                    if (rect.width >= searchRect.width
                        && rect.width <= viewportWidth * 0.48
                        && rect.height >= viewportHeight * 0.35
                        && rect.left <= searchRect.left + 24
                        && rect.right >= searchRect.right - 24) {
                        panel = node;
                        break;
                    }
                }
                const panelRect = panel ? panel.getBoundingClientRect() : {
                    left: Math.max(0, searchRect.left - 80),
                    right: Math.min(
                        viewportWidth * 0.50,
                        searchRect.right + Math.max(120, searchRect.width * 0.6)
                    ),
                    top: searchRect.top,
                    bottom: viewportHeight,
                    width: Math.min(viewportWidth * 0.50, searchRect.width * 1.8),
                    height: viewportHeight - searchRect.top
                };
                document.querySelectorAll('[data-sk-candidate-token]').forEach(
                    element => element.removeAttribute('data-sk-candidate-token')
                );
                const leaves = [...document.querySelectorAll('span,p,strong,div')]
                    .filter(element => {
                        leafCounts.examined++;
                        if (!visible(element)) { leafCounts.not_visible++; return false; }
                        if (element.children.length > 3) {
                            leafCounts.too_many_children++; return false;
                        }
                        const rect = element.getBoundingClientRect();
                        const text = normalize(element.innerText);
                        const descendantMatch = [...element.querySelectorAll(
                            'span,p,strong,div'
                        )].some(descendant =>
                            visible(descendant)
                            && normalize(descendant.innerText).includes(needle)
                        );
                        if (!text || !text.includes(needle)) {
                            leafCounts.query_mismatch++; return false;
                        }
                        if (descendantMatch) { leafCounts.descendant_match++; return false; }
                        if (text.length >= 180) { leafCounts.text_too_long++; return false; }
                        if (rect.left + rect.width / 2 < panelRect.left
                            || rect.left + rect.width / 2 > panelRect.right
                            || rect.bottom < searchRect.bottom - 12) {
                            leafCounts.outside_panel++; return false;
                        }
                        leafCounts.accepted++;
                        return true;
                    });
                const results = [];
                const used = new Set();
                for (const leaf of leaves) {
                    let row = leaf;
                    let selected = null;
                    for (let depth = 0;
                         depth < 8 && row && row !== document.body;
                         depth++, row = row.parentElement) {
                        const rect = row.getBoundingClientRect();
                        const text = normalize(row.innerText);
                        const insidePanel = rect.left >= panelRect.left - 24
                            && rect.right <= panelRect.right + 24
                            && rect.top >= searchRect.bottom - 16
                            && rect.bottom <= panelRect.bottom + 8;
                        const rowShape = rect.height >= 32
                            && rect.height <= 180
                            && rect.width >= Math.min(140, searchRect.width * 0.65)
                            && rect.width <= panelRect.width + 48;
                        rowCounts.examined++;
                        if (!visible(row)) { rowCounts.not_visible++; continue; }
                        if (!insidePanel) { rowCounts.outside_panel++; continue; }
                        if (!rowShape) { rowCounts.invalid_shape++; continue; }
                        if (text.length > 500) { rowCounts.text_too_long++; continue; }
                        const interactive = row.matches(
                            'a,button,li,[role="listitem"],[role="option"]'
                        ) || row.querySelector('a,button,[role="button"]');
                        const pointer = getComputedStyle(row).cursor === 'pointer';
                        const stableAttribute = [...row.attributes].some(attribute =>
                            /(uid|user.?id|conversation.?id)/i.test(attribute.name)
                            && Boolean(attribute.value)
                        );
                        const chatAction = [...row.querySelectorAll('*')].some(element =>
                            visible(element)
                            && getComputedStyle(element).cursor === 'pointer'
                            && /^(发消息|聊天)$/.test(normalize(element.innerText))
                        );
                        if (row.querySelector('img')
                            && (interactive || pointer || stableAttribute || chatAction)) {
                            selected = row;
                            break;
                        }
                        rowCounts.missing_avatar_or_action++;
                    }
                    if (!selected) { rowCounts.no_row++; continue; }
                    if (used.has(selected)) { rowCounts.duplicate_row++; continue; }
                    const text = normalize(selected.innerText);
                    const hasGroupCountSuffix = [
                        selected,
                        ...selected.querySelectorAll('span,p,strong,div')
                    ].some(element =>
                        element.children.length <= 3
                        && /[（(]\s*\d+\s*[）)]$/.test(normalize(element.innerText))
                    );
                    if (text.includes('群聊')
                        || /(?:^|\s)\d+\s*人(?:\s|$)/.test(text)
                        || hasGroupCountSuffix) {
                        rowCounts.group_text++;
                        continue;
                    }
                    used.add(selected);
                    const token = `candidate-${results.length}`;
                    selected.setAttribute('data-sk-candidate-token', token);
                    const links = [...selected.querySelectorAll('a[href]')]
                        .map(link => link.href)
                        .filter(Boolean);
                    const conversationIdentity = (__READ_CONVERSATION_IDENTITY__)(selected);
                    if (conversationIdentity && conversationIdentity.type !== null
                        && conversationIdentity.type !== 1) {
                        rowCounts.non_private_conversation++;
                        continue;
                    }
                    const participantProfile = conversationIdentity?.secUserId
                        ? `https://www.douyin.com/user/${conversationIdentity.secUserId}` : '';
                    const linkedProfile = links.find(href => href.includes('/user/')) || '';
                    if (participantProfile && linkedProfile
                        && new URL(linkedProfile).pathname.replace(/\/$/, '')
                            !== new URL(participantProfile).pathname) {
                        rowCounts.conflicting_profile++;
                        continue;
                    }
                    const profileUrl = participantProfile || linkedProfile;
                    const image = selected.querySelector('img');
                    const lines = (selected.innerText || '')
                        .split(/\n+/)
                        .map(normalize)
                        .filter(Boolean);
                    const name = lines.find(line => line === needle)
                        || lines.find(line => line.includes(needle))
                        || lines[0]
                        || needle;
                    const idLine = lines.find(line => /抖音号\s*[:：]/.test(line)) || '';
                    const idMatch = idLine.match(/抖音号\s*[:：]\s*([^\s]+)/);
                    const dataIds = [];
                    for (const node of [selected, ...selected.querySelectorAll('*')]) {
                        for (const attribute of node.attributes || []) {
                            if (/(uid|user.?id|conversation.?id)/i.test(attribute.name)
                                && attribute.value && attribute.value.length <= 200) {
                                dataIds.push(`${attribute.name}:${attribute.value}`);
                            }
                        }
                    }
                    results.push({
                        token,
                        name,
                        douyinId: idMatch ? idMatch[1] : '',
                        profileUrl,
                        avatarUrl: image ? (image.currentSrc || image.src || '') : '',
                        dataId: [...new Set(dataIds)].sort().join('|'),
                        lineCount: lines.length
                    });
                }
                return {
                    rows: results.slice(0, 20),
                    summary: {
                        search: {
                            placeholder: searchInput.getAttribute('placeholder') || '',
                            value: searchInput.value,
                            data_e2e: searchInput.getAttribute('data-e2e') || '',
                            focused: document.activeElement === searchInput,
                            disabled: searchInput.disabled,
                            read_only: searchInput.readOnly,
                            bounds: bounds(searchRect),
                            visible: visible(searchInput)
                        },
                        document_state: document.readyState,
                        panel_source: panel ? 'ancestor' : 'geometry_fallback',
                        panel_bounds: bounds(panelRect),
                        viewport: {width: viewportWidth, height: viewportHeight},
                        count_semantics: 'First rejection per examined DOM leaf or ancestor; '
                            + 'row counts are not unique people.',
                        leaf_counts: leafCounts,
                        row_counts: rowCounts,
                        accepted_rows: results.length,
                        returned_rows: Math.min(results.length, 20),
                        omitted_by_limit: Math.max(0, results.length - 20)
                    }
                };
            }""".replace("__READ_CONVERSATION_IDENTITY__", _CONVERSATION_IDENTITY_JS),
            query,
        )
        self._last_candidate_diagnostics = snapshot["summary"]
        return snapshot["rows"]

    @staticmethod
    def _candidate_from_raw(raw: dict[str, Any]) -> FriendCandidate:
        display_name = " ".join(str(raw.get("name") or "").split()).strip()
        douyin_id = " ".join(str(raw.get("douyinId") or "").split()).strip()
        profile_url = _canonical_http_url(str(raw.get("profileUrl") or ""))
        avatar_url = _canonical_http_url(str(raw.get("avatarUrl") or ""))
        data_id = str(raw.get("dataId") or "")[:500]
        if profile_url and "/user/self" not in profile_url:
            identity_source = f"profile:{profile_url}"
            strength = "strong"
        elif douyin_id:
            identity_source = f"douyin:{douyin_id}"
            strength = "strong"
        elif data_id:
            identity_source = f"data:{data_id}"
            strength = "strong"
        else:
            avatar_path = urlsplit(avatar_url).path if avatar_url else ""
            identity_source = f"visual:{display_name}|{avatar_path}"
            strength = "weak"
        stable_key = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()
        return FriendCandidate(
            stable_key=stable_key,
            display_name=display_name,
            douyin_id=douyin_id,
            profile_url=profile_url,
            avatar_url=avatar_url,
            evidence={
                "identity_strength": strength,
                "profile_url": profile_url,
                "douyin_id": douyin_id,
                "data_id_digest": hashlib.sha256(data_id.encode("utf-8")).hexdigest()
                if data_id
                else "",
                "line_count": int(raw.get("lineCount") or 0),
            },
        )

    @staticmethod
    def _identity_matches(candidate: FriendCandidate, target: Target) -> bool:
        return (
            candidate.stable_key == target.stable_key
            or bool(target.profile_url and candidate.profile_url == target.profile_url)
            or bool(target.douyin_id and candidate.douyin_id == target.douyin_id)
        )

    async def _matching_outgoing_count(self, text: str) -> int:
        return int(
            await self.page.evaluate(
                r"""expected => {
                    const normalize = value => (value || '').replace(/[\s\u200B-\u200D\uFEFF]+/g, ' ').trim();
                    const visible = element => {
                        const r = element.getBoundingClientRect();
                        const s = getComputedStyle(element);
                        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                    };
                    return [...document.querySelectorAll('div,span,p')].filter(element => {
                        if (!visible(element) || element.children.length > 3) return false;
                        const r = element.getBoundingClientRect();
                        return r.left + r.width / 2 > window.innerWidth * 0.55 && normalize(element.innerText) === normalize(expected);
                    }).length;
                }""",
                text,
            )
        )

    async def _sample_delivery(self, text: str, before_count: int) -> dict[str, bool]:
        return await self.page.evaluate(
            r"""({expected, beforeCount}) => {
                const normalize = value => (value || '').replace(/[\s\u200B-\u200D\uFEFF]+/g, ' ').trim();
                const visible = element => {
                    const r = element.getBoundingClientRect();
                    const s = getComputedStyle(element);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                const matches = [...document.querySelectorAll('div,span,p')].filter(element => {
                    if (!visible(element) || element.children.length > 3) return false;
                    const r = element.getBoundingClientRect();
                    return r.left + r.width / 2 > window.innerWidth * 0.55 && normalize(element.innerText) === normalize(expected);
                });
                if (matches.length <= beforeCount) return {newMatchingOutgoing:false, pending:false, failed:false};
                let scope = matches[matches.length - 1];
                for (let i = 0; i < 6 && scope.parentElement; i++) {
                    const parent = scope.parentElement;
                    const rect = parent.getBoundingClientRect();
                    if (rect.height > 260 || rect.width > window.innerWidth * 0.75) break;
                    scope = parent;
                }
                const failure = [...scope.querySelectorAll('*')].some(element => {
                    if (!visible(element)) return false;
                    const label = `${element.getAttribute('aria-label') || ''} ${element.getAttribute('title') || ''} ${element.innerText || ''}`;
                    const classes = String(element.className || '');
                    return /发送失败|重试/.test(label) || /(send.?fail|retry)/i.test(classes);
                });
                const pending = [...scope.querySelectorAll('*')].some(element => {
                    if (!visible(element)) return false;
                    const classes = String(element.className || '');
                    return element.getAttribute('role') === 'progressbar' || element.getAttribute('aria-busy') === 'true' || /(spin|loading|pending)/i.test(classes);
                });
                return {newMatchingOutgoing:true, pending, failed:failure};
            }""",
            {"expected": text, "beforeCount": before_count},
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[\s\u200B-\u200D\uFEFF]+", " ", value).strip()


async def derive_account_identity(session: BrowserSession) -> Account:
    """从当前内存会话计算不可逆账号摘要，不持久化登录凭据。"""

    state = await session.context.storage_state()
    return await _derive_account(session, state)


async def interactive_login(
    factory: BrowserSessionFactory,
    *,
    status: _STATUS_CALLBACK | None = None,
) -> LoginResult:
    async with factory.open(headless=False, use_saved_state=False) as session:
        adapter = DouyinChatAdapter(session.page)
        await adapter.wait_for_interactive_login(status)
        state = await session.context.storage_state()
        account = await _derive_account(session, state)
        await factory.save_state(session.context)
        return LoginResult(account)


async def _derive_account(session: BrowserSession, state: dict[str, Any]) -> Account:
    profile_href = await session.page.evaluate(
        r"""() => {
            const links = [...document.querySelectorAll('a[href]')];
            const mine = links.find(link => (link.innerText || '').trim() === '我的' && link.href.includes('/user/'));
            return mine ? mine.href : '';
        }"""
    )
    canonical_profile = _canonical_http_url(str(profile_href or ""))
    identity_material = canonical_profile
    if not identity_material or "/user/self" in identity_material:
        cookies = list(state.get("cookies", [])) if isinstance(state, dict) else []
        account_cookie_values = [
            str(cookie.get("value"))
            for cookie in cookies
            if isinstance(cookie, dict)
            and str(cookie.get("name", "")).casefold() in {"uid_tt", "uid_tt_ss", "sid_tt"}
            and cookie.get("value")
        ]
        identity_material = "|".join(sorted(account_cookie_values))
    if not identity_material:
        identity_material = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    platform_user_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
    return Account(platform_user_id=platform_user_id, display_name="已登录账号", logged_in_at="")


def _canonical_http_url(value: str) -> str:
    if not value:
        return ""
    try:
        absolute = urljoin(CHAT_URL, value)
        parsed = urlsplit(absolute)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
