from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from html import escape
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from spark_keeper.automation.douyin_chat import _CONVERSATION_IDENTITY_JS, DouyinChatAdapter
from spark_keeper.automation.errors import (
    AuthenticationRequired,
    AutomationError,
    HumanVerificationRequired,
    TargetIdentityMismatch,
)
from spark_keeper.automation.service import BatchService
from spark_keeper.automation.spark_scan import _SNAPSHOT_JS, RECOGNITION_LIMIT, scan_contacts
from spark_keeper.models import Account, ErrorCode, SparkScanStatus, SparkState, Target

ACCOUNT = Account("fixture-account", "测试账号", "2026-09-07T12:00:00+08:00")
END = '<div role="status" aria-label="会话列表已全部加载">末端</div>'


def row(
    identity: str = "friend",
    *,
    name: str = "同名好友",
    badge: str = "火花已点亮",
    chat_type: str = "single",
    extra: str = "",
    profile: str | None = None,
) -> str:
    # Synthetic accessibility-contract fixture, NOT a capture of Douyin's live DOM.
    href = profile if profile is not None else f"https://www.douyin.com/user/{identity}"
    heading = f'<span data-role="conversation-name">{escape(name)}</span>'
    if identity:
        heading = f'<a href="{escape(href, quote=True)}">{heading}</a>'
    icon = f'<span role="img" aria-label="{escape(badge, quote=True)}">icon</span>' if badge else ""
    return f'''<li data-conversation-type="{escape(chat_type, quote=True)}">
        <div data-role="conversation-header">{heading}{icon}{extra}</div>
        <p data-role="message-preview">private-preview 火花 🔥
        <span role="img" aria-label="火花已点亮">private-message-icon</span></p>
    </li>'''


def html(rows: str, *, end: bool = True) -> str:
    return (
        """<style>
        body {margin:0} aside {width:360px;height:760px} input {width:300px;height:30px}
        ul {margin:0;padding:0;position:relative;height:660px;overflow-y:auto}
        li {height:76px;list-style:none} p {margin:0} [role=img] {display:inline-block;width:30px;height:20px}
        [role=status] {height:26px} main {position:absolute;left:450px;top:400px}
    </style><aside><input placeholder="搜索好友"><ul id="list" role="list" aria-label="会话列表">"""
        + rows
        + (END if end else "")
        + """</ul></aside><main><textarea id="composer"></textarea>
        <button id="send" onclick="window.sendCount++">发送</button></main>
        <script>window.sendCount=0</script>"""
    )


@asynccontextmanager
async def fixture_page(content: str):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(content)
            yield page
        finally:
            await browser.close()


async def scan(page, **kwargs):
    return await scan_contacts(DouyinChatAdapter(page), ACCOUNT, load_wait_ms=35, **kwargs)


@pytest.mark.asyncio
async def test_explicit_badges_only_ignore_names_messages_and_opaque_icons() -> None:
    rows = (
        row("active")
        + row("inactive", badge="火花已熄灭")
        + row("emoji", name="火花已点亮 🔥", badge="")
        + row(
            "opaque",
            badge="",
            extra='<svg style="color:orange" width="20" height="20"><path d="M0 0L20 20"/></svg>',
        )
        + row("conflict", extra='<span role="img" aria-label="火花已熄灭">icon</span>')
        + row("restore", badge="火花恢复中")
        + row("gray", badge="灰色有效火花")
    )
    async with fixture_page(html(rows)) as page:
        result = await scan(page)
        assert result.status is SparkScanStatus.COMPLETE
        assert [contact.spark_state for contact in result.contacts] == [
            SparkState.ACTIVE,
            SparkState.INACTIVE,
            SparkState.UNKNOWN,
            SparkState.UNKNOWN,
            SparkState.UNKNOWN,
            SparkState.RECOVER,
            SparkState.ACTIVE,
        ]
        assert all(contact.importable for contact in result.contacts)
        assert RECOGNITION_LIMIT in result.detail
        assert "private-preview" not in repr(result)
        snapshot = await page.evaluate(_SNAPSHOT_JS, {"operation": "read", "limit": 1000})
        assert "private-preview" not in repr(snapshot)
        assert "private-message-icon" not in repr(snapshot)
        assert await page.locator("#composer").input_value() == ""
        assert await page.evaluate("window.sendCount") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("badge", "extra_badge", "expected"),
    [
        ("火花已置灰", "", SparkState.ACTIVE),
        ("灰色火花", "", SparkState.ACTIVE),
        ("火花待恢复", "", SparkState.RECOVER),
        ("火花重燃中", "", SparkState.RECOVER),
        ("火花已置灰", "火花恢复中", SparkState.UNKNOWN),
        ("火花恢复中", "火花已熄灭", SparkState.UNKNOWN),
        ("火花已置灰", "未知火花状态", SparkState.UNKNOWN),
    ],
)
async def test_gray_and_recover_badge_semantics_stay_distinct(badge, extra_badge, expected) -> None:
    extra = f'<span role="img" aria-label="{extra_badge}">icon</span>' if extra_badge else ""
    async with fixture_page(html(row(badge=badge, extra=extra))) as page:
        result = await scan(page)
        assert result.contacts[0].spark_state is expected
        assert result.contacts[0].importable


@pytest.mark.asyncio
@pytest.mark.parametrize("badge", ["火花已点亮", "灰色有效火花", "火花待恢复", "火花已熄灭", ""])
async def test_groups_unknown_types_weak_and_conflicting_identities_cannot_import(badge) -> None:
    rows = (
        row("group", badge=badge, chat_type="group")
        + row("unknown", badge=badge, chat_type="")
        + row("", badge=badge, name="重复弱名称")
        + row("", badge=badge, name="重复弱名称")
        + row("foreign", badge=badge, profile="https://evil.example/user/foreign")
        + row(
            "conflict",
            badge=badge,
            extra='<a aria-label="查看主页" href="https://www.douyin.com/user/other">主页</a>',
        )
    )
    async with fixture_page(html(rows)) as page:
        result = await scan(page)
        assert len(result.contacts) == 6
        assert not any(contact.importable for contact in result.contacts)
        assert result.contacts[0].is_group
        assert result.contacts[1].candidate.evidence["chat_type"] == "unknown"
        assert result.contacts[2].candidate.stable_key != result.contacts[3].candidate.stable_key
        assert result.contacts[-1].candidate.evidence["identity_conflict"]


@pytest.mark.asyncio
@pytest.mark.parametrize("badge", ["火花已点亮", "灰色有效火花"])
async def test_same_name_different_stable_ids_remain_distinct(badge) -> None:
    async with fixture_page(
        html(row("one", badge=badge) + row("two", badge=badge) + row("one", badge=badge))
    ) as page:
        result = await scan(page)
        assert result.scanned_count == 2
        assert len({contact.candidate.stable_key for contact in result.contacts}) == 2
        assert all(contact.importable for contact in result.contacts)


@pytest.mark.asyncio
async def test_gray_and_recover_observations_of_same_identity_remain_unknown() -> None:
    async with fixture_page(
        html(row("same", badge="灰色有效火花") + row("same", badge="火花恢复中"))
    ) as page:
        result = await scan(page)
        assert len(result.contacts) == 1
        assert result.contacts[0].spark_state is SparkState.UNKNOWN
        assert result.contacts[0].importable


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["douyin-id", "unknown-type", "group-type"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_same_profile_identity_or_type_conflict_blocks_manual_import(
    conflict, reverse
) -> None:
    first = row("same").replace("<li ", '<li data-douyin-id="first_id" ', 1)
    second_id = "other_id" if conflict == "douyin-id" else "first_id"
    second = row(
        "same",
        badge="火花待恢复",
        chat_type={"unknown-type": "", "group-type": "group"}.get(conflict, "single"),
    ).replace(
        "<li ",
        f'<li data-douyin-id="{second_id}" ',
        1,
    )
    rows = [first, second]
    if reverse:
        rows.reverse()
    async with fixture_page(html("".join(rows) + first)) as page:
        result = await scan(page)
        assert len(result.contacts) == 1
        contact = result.contacts[0]
        assert contact.spark_state is SparkState.UNKNOWN
        assert contact.candidate.evidence["identity_conflict"]
        assert not contact.importable
        assert await page.evaluate("window.sendCount") == 0


@pytest.mark.asyncio
async def test_virtual_list_scrolls_and_deduplicates_overlapping_rows() -> None:
    async with fixture_page(html("", end=False)) as page:
        await page.evaluate(
            """({rows, end}) => {
            const list = document.querySelector('#list');
            list.style.height = '180px';
            function render() {
                const top = list.scrollTop;
                const index = Math.min(Math.floor(top / 130), 2);
                list.innerHTML = '<div style="height:900px"></div>';
                const pane = document.createElement('div');
                pane.style.cssText = `position:absolute;left:0;right:0;top:${top}px`;
                pane.innerHTML = rows[index] + rows[index + 1] + (index === 2 ? end : '');
                list.appendChild(pane);
            }
            list.addEventListener('scroll', render);
            render();
        }""",
            {"rows": [row(str(index)) for index in range(4)], "end": END},
        )
        result = await scan(page, max_rounds=10)
        assert result.status is SparkScanStatus.COMPLETE
        assert result.scanned_count == 4
        assert len({contact.candidate.stable_key for contact in result.contacts}) == 4
        assert await page.evaluate("window.sendCount") == 0


@pytest.mark.asyncio
async def test_no_end_marker_stagnation_is_partial_not_empty_success() -> None:
    async with fixture_page(html(row(), end=False)) as page:
        result = await scan(page)
        assert result.status is SparkScanStatus.PARTIAL
        assert result.scanned_count == 1
        assert "停滞" in result.detail


@pytest.mark.asyncio
async def test_loading_and_limits_never_claim_complete() -> None:
    async with fixture_page(html(row("one") + row("two"))) as page:
        result = await scan(page, max_contacts=1)
        assert result.status is SparkScanStatus.PARTIAL
        assert result.scanned_count == 1
        await page.locator("#list").evaluate("node => node.setAttribute('aria-busy', 'true')")
        result = await scan(page, max_rounds=2)
        assert result.status is SparkScanStatus.PARTIAL
        result = await scan(page, timeout_seconds=0.001)
        assert result.status is SparkScanStatus.PARTIAL
        assert "时间耗尽" in result.detail


@pytest.mark.asyncio
async def test_cancel_retains_observations_and_progress_is_count_only() -> None:
    cancel = threading.Event()
    progress = []

    def on_progress(message):
        progress.append(message)
        cancel.set()

    async with fixture_page(html(row("one"))) as page:
        result = await scan(page, cancel=cancel, progress=on_progress)
        assert result.status is SparkScanStatus.CANCELLED
        assert result.scanned_count == 1
        assert all("同名好友" not in message and "one" not in message for message in progress)
        result = await scan(page, cancel=cancel)
        assert result.status is SparkScanStatus.CANCELLED
        assert result.contacts == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "error"),
    [
        (
            '<input placeholder="请输入手机号"><div>扫码登录</div><div>密码登录</div>',
            AuthenticationRequired,
        ),
        ("<div>请完成安全验证</div>", HumanVerificationRequired),
    ],
)
async def test_login_and_verification_stop_fatally(content, error) -> None:
    async with fixture_page(content) as page:
        with pytest.raises(error):
            await scan(page)


@pytest.mark.asyncio
async def test_unrecognized_list_is_partial() -> None:
    async with fixture_page(
        html(row()).replace('aria-label="会话列表"', 'aria-label="未知列表"')
    ) as page:
        result = await scan(page)
        assert result.status is SparkScanStatus.PARTIAL
        assert not result.contacts


@pytest.mark.asyncio
async def test_service_scan_uses_html_and_never_calls_send_or_mutates_targets(monkeypatch) -> None:
    async with fixture_page(html(row())) as page:
        account = ACCOUNT
        events = []
        database = SimpleNamespace(
            get_account=lambda: account, record_event=lambda *args: events.append(args)
        )
        service = BatchService(database, SimpleNamespace(exists=lambda: True))

        @asynccontextmanager
        async def open_fixture(**_kwargs):
            yield SimpleNamespace(page=page)

        async def open_chat(adapter):
            await adapter._require_ready()

        async def derive(_session):
            return ACCOUNT

        async def forbidden(*_args, **_kwargs):
            pytest.fail("Read-only scan called a recipient/send operation")

        service.browser_factory = SimpleNamespace(open=open_fixture)
        monkeypatch.setattr(DouyinChatAdapter, "open_chat", open_chat)
        monkeypatch.setattr(DouyinChatAdapter, "send_text_and_confirm", forbidden)
        monkeypatch.setattr(DouyinChatAdapter, "open_confirmed_target", forbidden)
        monkeypatch.setattr(DouyinChatAdapter, "search_targets", forbidden)
        monkeypatch.setattr("spark_keeper.automation.service.derive_account_identity", derive)
        result = await service.scan_spark_contacts()
        assert result.account_key == ACCOUNT.platform_user_id
        assert result.account_logged_in_at == ACCOUNT.logged_in_at
        assert result.scanned_count == 1
        assert events and "同名好友" not in repr(events)
        assert await page.evaluate("window.sendCount") == 0

        def change_account(_message):
            nonlocal account
            account = replace(ACCOUNT, logged_in_at="different-login-generation")

        with pytest.raises(AutomationError) as caught:
            await service.scan_spark_contacts(progress=change_account)
        assert caught.value.code is ErrorCode.CONFIGURATION_INVALID


@pytest.mark.asyncio
async def test_row_id_alone_is_pending_but_explicit_douyin_id_can_import() -> None:
    weak = row("").replace("<li ", '<li data-uid="opaque-row-id" ', 1)
    strong = row("").replace("<li ", '<li data-douyin-id="friend_123" ', 1)
    async with fixture_page(html(weak + strong)) as page:
        result = await scan(page)
        assert len(result.contacts) == 2
        assert not result.contacts[0].importable
        assert result.contacts[0].candidate.evidence["identity_strength"] == "weak"
        assert result.contacts[1].importable
        assert result.contacts[1].candidate.douyin_id == "friend_123"


@pytest.mark.asyncio
async def test_preview_metadata_cannot_supply_identity_type_badge_or_list_end() -> None:
    content = row("", badge="", chat_type="").replace(
        '<p data-role="message-preview">',
        '<p data-role="message-preview"><a aria-label="查看主页" '
        'href="https://www.douyin.com/user/message-author">message link</a>'
        '<span role="img" aria-label="单聊">type</span>'
        '<span role="status" aria-label="会话列表已全部加载">fake end</span>',
    )
    async with fixture_page(html(content, end=False)) as page:
        result = await scan(page)
        assert result.status is SparkScanStatus.PARTIAL
        assert len(result.contacts) == 1
        contact = result.contacts[0]
        assert contact.spark_state is SparkState.UNKNOWN
        assert contact.candidate.evidence["chat_type"] == "unknown"
        assert not contact.candidate.profile_url
        assert not contact.importable


@pytest.mark.asyncio
async def test_observed_class_list_is_scanned_without_synthetic_aria_or_data_roles() -> None:
    # Structure from the authorized metadata-only probe; all values are invented.
    content = """<style>
        .conversationConversationListwrapper {height:360px;width:350px;overflow:auto}
        .conversationConversationItemwrapper {height:80px}
        .commonStreakicon {width:20px;height:20px}
    </style><input placeholder="搜索好友">
    <div class="conversationConversationListwrapper">
      <div class="conversationConversationItemwrapper">
        <div class="conversationConversationItemtitleWrapper">
          <div class="conversationConversationItemtitle"><span>待确认好友</span></div>
          <div class="ConversationItemTagNextToTitlewrapper">
            <div class="commonStreakstreakContainer">
              <img class="commonStreakicon" alt="" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E">
              <div class="commonStreaknormalText">99</div>
            </div>
          </div>
        </div>
        <div class="ConversationItemDescwrapper"><pre class="ConversationItemHinttextBox">private-preview 🔥</pre></div>
      </div>
    </div>"""
    async with fixture_page(content) as page:
        result = await scan(page)
        assert result.status is SparkScanStatus.PARTIAL
        assert len(result.contacts) == 1
        contact = result.contacts[0]
        assert contact.candidate.display_name == "待确认好友"
        assert contact.spark_state is SparkState.UNKNOWN
        assert not contact.importable
        assert "private-preview" not in repr(result)


@pytest.mark.asyncio
async def test_scan_and_search_share_row_bound_participant_identity() -> None:
    content = html(
        row(
            "",
            extra='<img width="24" height="24" alt="" src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\'/%3E">',
        )
    )
    async with fixture_page(content) as page:
        await page.locator("li").evaluate("""node => {
            node.classList.add('conversationConversationItemwrapper');
            const conversation = {
                get toParticipantSecUserId() { throw Error('Must not call cache-writing getter'); },
                get toParticipantUserId() { return 'fixture-other-uid'; },
                firstPageParticipant: {participants: [
                    {user_id: 'fixture-self-uid', sec_uid: 'fixture-self-sec'},
                    {user_id: 'fixture-other-uid', sec_uid: 'fixture-sec-user'}
                ]},
                get type() { return 1; },
                get coreInfo() { throw Error('Must not read unrelated conversation fields'); },
                get ext() { throw Error('Must not read unrelated conversation fields'); }
            };
            node.__reactFiber$fixture = {memoizedProps: {conversation}, return: null};
        }""")
        result = await scan(page)
        candidate = result.contacts[0].candidate
        assert candidate.profile_url == "https://www.douyin.com/user/fixture-sec-user"
        assert result.contacts[0].importable
        # The observed search-result component wraps this same conversation in
        # props.item, whereas the scanned conversation row exposes it directly.
        await page.locator("li").evaluate("""node => {
            const fiber = node.__reactFiber$fixture;
            fiber.memoizedProps = {item: {
                conversation: fiber.memoizedProps.conversation,
                prefix: '', highlight: '同名好友', suffix: ''
            }};
        }""")
        adapter = DouyinChatAdapter(page)
        rows = await adapter._extract_candidate_rows("同名好友", page.locator("input"))
        assert len(rows) == 1
        searched = adapter._candidate_from_raw(rows[0])
        assert searched.stable_key == candidate.stable_key
        assert searched.profile_url == candidate.profile_url
        target = Target(
            id=1,
            stable_key=candidate.stable_key,
            display_name=candidate.display_name,
            douyin_id=candidate.douyin_id,
            profile_url=candidate.profile_url,
            avatar_url=candidate.avatar_url,
            search_query=candidate.display_name,
            evidence=candidate.evidence,
            enabled=True,
            confirmed_at="now",
        )
        assert adapter._identity_matches(searched, target)
        assert "private-preview" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "badge", "expected"),
    [
        (1, "", SparkState.ACTIVE),
        (2, "", SparkState.ACTIVE),
        (3, "", SparkState.RECOVER),
        (4, "", SparkState.INACTIVE),
        (9, "", SparkState.UNKNOWN),
        (2, "灰色有效火花", SparkState.ACTIVE),
        (3, "火花恢复中", SparkState.RECOVER),
        (2, "火花恢复中", SparkState.UNKNOWN),
        (3, "灰色有效火花", SparkState.UNKNOWN),
        (2, "火花已熄灭", SparkState.UNKNOWN),
        (4, "火花恢复中", SparkState.UNKNOWN),
        (3, "火花已熄灭", SparkState.UNKNOWN),
        (2, "未知火花状态", SparkState.UNKNOWN),
    ],
)
async def test_source_verified_flame_state_not_icon_or_days_determines_status(
    state, badge, expected
) -> None:
    icon = """<div class="commonStreakstreakContainer">
        <span class="commonStreakicon">same-opaque-icon</span>
        <div class="commonStreaknormalText">99</div></div>"""
    async with fixture_page(html(row("", badge=badge, extra=icon))) as page:
        await page.locator("li").evaluate(
            """(node, state) => {
            Date.now = () => 1800000000000;
            node.classList.add('conversationConversationItemwrapper');
            const ext = {'a:consecutive_chat_data': JSON.stringify({
                flame_infos: [{start: 1799999000, end: 1800001000, state, days: 99}]
            })};
            Object.defineProperty(ext, 'private-message', {get() {throw Error('Not whitelisted');}});
            node.__reactFiber$fixture = {memoizedProps: {conversation: {
                type: 1, toParticipantUserId: 'other',
                firstPageParticipant: {participants: [{user_id: 'other', sec_uid: 'fixture-sec'}]},
                coreInfo: {ext}
            }}, return: null};
        }""",
            state,
        )
        result = await scan(page)
        assert result.contacts[0].spark_state is expected
        assert result.contacts[0].importable
        assert "private-preview" not in repr(result)
        assert await page.evaluate("window.sendCount") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flame_infos",
    [
        [{"start": 1700000000, "end": 1700001000, "state": 1}],
        [{"start": 1900000000, "end": 1900001000, "state": 1}],
        [{"start": 1700000000, "end": 1700001000, "state": 2}],
        [{"start": 1900000000, "end": 1900001000, "state": 2}],
        [{"start": 1700000000, "end": 1700001000, "state": 3}],
        [{"start": 1900000000, "end": 1900001000, "state": 3}],
        [
            {"start": 1799999000, "end": 1800001000, "state": 1},
            {"start": 1799999000, "end": 1800001000, "state": 3},
        ],
        [
            {"start": 1799999000, "end": 1800001000, "state": 2},
            {"start": 1799999000, "end": 1800001000, "state": 3},
        ],
        [
            {"start": 1799999000, "end": 1800001000, "state": 1},
            {"start": 1799999000, "end": 1800001000, "state": 2},
        ],
    ],
)
async def test_expired_future_or_conflicting_flame_windows_are_unknown(flame_infos) -> None:
    icon = '<div class="commonStreakstreakContainer"><span>99</span></div>'
    async with fixture_page(html(row("", badge="", extra=icon))) as page:
        await page.locator("li").evaluate(
            """(node, flame_infos) => {
            Date.now = () => 1800000000000;
            node.classList.add('conversationConversationItemwrapper');
            node.__reactFiber$fixture = {memoizedProps: {conversation: {
                type: 1, toParticipantUserId: 'other',
                firstPageParticipant: {participants: [{user_id: 'other', sec_uid: 'fixture-sec'}]},
                coreInfo: {ext: {'a:consecutive_chat_data': JSON.stringify({flame_infos})}}
            }}, return: null};
        }""",
            flame_infos,
        )
        result = await scan(page)
        assert result.contacts[0].spark_state is SparkState.UNKNOWN
        assert result.contacts[0].importable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation_type", "importable"),
    [(1, True), (2, False), (3, False), (4, False), (17, False)],
)
async def test_official_numeric_conversation_types_gate_import(
    conversation_type, importable
) -> None:
    async with fixture_page(html(row("", chat_type=""))) as page:
        await page.locator("li").evaluate(
            """(node, type) => {
            node.classList.add('conversationConversationItemwrapper');
            node.__reactFiber$fixture = {memoizedProps: {conversation: {
                type, toParticipantUserId: 'other',
                firstPageParticipant: {participants: [{user_id: 'other', sec_uid: 'fixture-sec'}]}
            }}, return: null};
        }""",
            conversation_type,
        )
        result = await scan(page)
        assert result.contacts[0].importable is importable
        assert result.contacts[0].is_group is (conversation_type == 2)


@pytest.mark.asyncio
async def test_current_chat_verifies_shared_participant_identity_and_rejects_weak_fallback() -> (
    None
):
    content = """<style>
      input {position:absolute;left:40px;top:80px;width:240px;height:36px}
      .pane {position:absolute;left:400px;top:0;width:760px;height:780px}
      .header {position:absolute;left:50px;top:70px;width:650px;height:80px}
      .header img {width:42px;height:42px}
      .composer {position:absolute;left:60px;top:650px;width:620px;height:60px}
    </style><input placeholder="搜索已有会话"><main class="pane">
      <div class="header RightPanelHeadertitleContainer">
        <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"><span>同名好友</span>
      </div><div class="composer" contenteditable="true"></div></main>"""
    async with fixture_page(content) as page:
        await page.locator(".header").evaluate("""node => {
            node.__reactFiber$fixture = {memoizedProps: {conversation: {
                type: 1, toParticipantUserId: 'other',
                firstPageParticipant: {participants: [{user_id: 'other', sec_uid: 'fixture-sec'}]}
            }}, return: null};
        }""")
        adapter = DouyinChatAdapter(page)
        candidate = await adapter.capture_current_chat_candidate("同名好友")
        assert candidate.profile_url == "https://www.douyin.com/user/fixture-sec"
        target = Target(
            id=1,
            stable_key=candidate.stable_key,
            display_name=candidate.display_name,
            douyin_id="",
            profile_url=candidate.profile_url,
            avatar_url="",
            search_query="同名好友",
            evidence={"capture_source": "spark_scan", "identity_strength": "strong"},
            enabled=False,
            confirmed_at="fixture-time",
        )
        await adapter.verify_recipient(target)
        await page.locator(".header").evaluate("node => delete node.__reactFiber$fixture")
        with pytest.raises(TargetIdentityMismatch):
            await adapter.verify_recipient(target)
        assert await page.locator(".composer").inner_text() == ""


@pytest.mark.asyncio
async def test_virtual_groups_use_conversation_digest_without_upgrading_identity() -> None:
    async with fixture_page(html("", end=False)) as page:
        await page.evaluate(
            """({row, end}) => {
            const list = document.querySelector('#list');
            list.style.height = '180px';
            function render() {
                const top = list.scrollTop;
                const index = Math.min(Math.floor(top / 130), 2);
                list.innerHTML = '<div style="height:900px"></div>';
                const pane = document.createElement('div');
                pane.style.cssText = `position:absolute;left:0;right:0;top:${top}px`;
                pane.innerHTML = row + row + (index === 2 ? end : '');
                list.appendChild(pane);
                pane.querySelectorAll('li').forEach((node, offset) => {
                    node.classList.add('conversationConversationItemwrapper');
                    node.__reactFiber$fixture = {memoizedProps: {conversation: {
                        type: 2, id: `private-conversation-${index + offset}`
                    }}, return: null};
                });
            }
            list.addEventListener('scroll', render);
            render();
        }""",
            {"row": row("", name="重复群名", chat_type="group"), "end": END},
        )
        result = await scan(page, max_rounds=10)
        assert result.status is SparkScanStatus.COMPLETE
        assert result.scanned_count == 4
        assert all(contact.is_group and not contact.importable for contact in result.contacts)
        assert all(
            contact.candidate.evidence["identity_strength"] == "weak" for contact in result.contacts
        )
        assert (
            len(
                {
                    contact.candidate.evidence["scan_conversation_digest"]
                    for contact in result.contacts
                }
            )
            == 4
        )
        assert "private-conversation-" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["direct", "item"])
async def test_stale_alternate_conversation_cannot_supply_recipient_identity(binding) -> None:
    async with fixture_page('<div class="conversationConversationItemwrapper"></div>') as page:
        node = page.locator(".conversationConversationItemwrapper")
        await node.evaluate(
            """(node, binding) => {
            const current = {
                id: 'current-conversation', type: 1, toParticipantUserId: 'peer',
                firstPageParticipant: {participants: [{user_id: 'peer', sec_uid: 'current-sec'}]}
            };
            const props = conversation => binding === 'item'
                ? {item: {conversation}} : {conversation};
            node.__reactFiber$fixture = {
                memoizedProps: props(current),
                alternate: {memoizedProps: props({id: 'previous-conversation', type: 1})},
                return: null
            };
        }""",
            binding,
        )
        assert await node.evaluate(_CONVERSATION_IDENTITY_JS) is None
        await node.evaluate("""node => {
            const props = node.__reactFiber$fixture.alternate.memoizedProps;
            (props.conversation || props.item.conversation).id = 'current-conversation';
        }""")
        assert (await node.evaluate(_CONVERSATION_IDENTITY_JS))["secUserId"] == "current-sec"
        await node.evaluate("""node => {
            const props = node.__reactFiber$fixture.alternate.memoizedProps;
            (props.conversation || props.item.conversation).type = 2;
        }""")
        assert await node.evaluate(_CONVERSATION_IDENTITY_JS) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("current", None),
        ("missing_binding", "host_boundary"),
        ("missing_participants", "participant_identity_missing"),
        ("outside_header", "host_boundary"),
        ("stale_alternate", "alternate_conflict"),
        ("stale_with_profile_link", "alternate_conflict"),
        ("wrong_participant", "participant_identity"),
        ("conflicting_link", "participant_identity"),
    ],
)
async def test_live_header_cur_conversation_binding_is_bounded_and_fail_closed(
    scenario, reason
) -> None:
    # Minimal observed 2026-09-08 live header hierarchy, not a page/message capture.
    content = """<style>
      input {position:absolute;left:40px;top:80px;width:240px;height:36px}
      .componentsRightPanelwrapper {position:absolute;left:400px;top:0;width:760px;height:780px}
      .RightPanelHeaderconvHeader {position:absolute;left:50px;top:70px;width:650px;height:80px}
      .composer {position:absolute;left:60px;top:650px;width:620px;height:60px}
    </style><input placeholder="搜索已有会话">
    <aside><a href="https://www.douyin.com/user/fixture-sec">同名好友</a></aside>
    <main class="componentsRightPanelwrapper">
      <div class="RightPanelHeaderconvHeader"><div class="RightPanelHeaderinfoContainer">
        <div class="RightPanelHeaderuser"><div class="RightPanelHeadersummatyBox">
          <div class="RightPanelHeadertitleContainer"><span>同名好友</span></div>
        </div></div>
      </div></div>
      <div class="composer" contenteditable="true"></div>
      <button onclick="window.sendCount++">发送</button>
    </main><script>window.sendCount=0</script>"""
    async with fixture_page(content) as page:
        await page.locator(".RightPanelHeaderconvHeader").evaluate(
            """(header, scenario) => {
                const conversation = {
                    id: 'current-conversation', type: 1, toParticipantUserId: 'peer',
                    firstPageParticipant: {participants: [
                        {user_id: 'self', sec_uid: 'self-sec'},
                        {user_id: 'peer', sec_uid: scenario === 'wrong_participant'
                            ? 'different-sec' : 'fixture-sec'}
                    ]}
                };
                if (scenario === 'missing_participants') delete conversation.firstPageParticipant;
                const pane = header.parentElement;
                const paneFiber = {stateNode: pane, memoizedProps: {curConversation: conversation}};
                const component = {
                    memoizedProps: scenario === 'missing_binding' || scenario === 'outside_header'
                        ? {} : {curConversation: conversation},
                    alternate: {memoizedProps: {curConversation:
                        scenario.startsWith('stale_')
                            ? {...conversation, id: 'previous-conversation'} : conversation}},
                    return: paneFiber
                };
                header.__reactFiber$fixture = {
                    stateNode: header, memoizedProps: {className: header.className}, return: component
                };
                let parentFiber = header.__reactFiber$fixture;
                for (const selector of [
                    '.RightPanelHeaderinfoContainer', '.RightPanelHeaderuser',
                    '.RightPanelHeadersummatyBox', '.RightPanelHeadertitleContainer'
                ]) {
                    const node = header.querySelector(selector);
                    node.__reactFiber$fixture = {
                        stateNode: node, memoizedProps: {className: node.className}, return: parentFiber
                    };
                    parentFiber = node.__reactFiber$fixture;
                }
                if (scenario === 'conflicting_link' || scenario === 'stale_with_profile_link') {
                    const link = document.createElement('a');
                    link.href = 'https://www.douyin.com/user/' +
                        (scenario === 'conflicting_link' ? 'different-sec' : 'fixture-sec');
                    header.appendChild(link);
                }
            }""",
            scenario,
        )
        adapter = DouyinChatAdapter(page)
        expected = adapter._candidate_from_raw({
            "name": "同名好友", "profileUrl": "https://www.douyin.com/user/fixture-sec"
        })
        target = Target(
            id=1, stable_key=expected.stable_key, display_name=expected.display_name,
            douyin_id="", profile_url=expected.profile_url, avatar_url="",
            search_query="同名好友",
            evidence={"capture_source": "spark_scan", "identity_strength": "strong"},
            enabled=False, confirmed_at="fixture-time",
        )
        if reason is None:
            await adapter.verify_recipient(target)
            candidate = await adapter.capture_current_chat_candidate("同名好友")
            assert candidate.profile_url == target.profile_url
            assert candidate.evidence["identity_strength"] == "strong"
        else:
            with pytest.raises(TargetIdentityMismatch) as caught:
                await adapter.verify_recipient(target)
            diagnostic = json.loads(caught.value.__notes__[0])
            assert diagnostic["stage"] == "current_chat_identity"
            assert diagnostic["extraction"]["conversation"]["reason"] == reason
            assert diagnostic["extraction"]["conversation"]["root_class"] == "RightPanelHeaderconvHeader"
        assert await page.locator(".composer").inner_text() == ""
        assert await page.evaluate("window.sendCount") == 0
