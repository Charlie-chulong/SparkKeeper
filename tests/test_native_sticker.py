from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Page, Route, async_playwright

from spark_keeper.automation import douyin_chat
from spark_keeper.automation.douyin_chat import (
    SPARK_STICKER_RESOURCE_PATH,
    DouyinChatAdapter,
)
from spark_keeper.automation.errors import (
    PageStructureChanged,
    SendFailed,
    SendUnknown,
    TargetIdentityMismatch,
)
from spark_keeper.automation.send_state import (
    DeliveryOutcome,
    DeliverySample,
    await_delivery_terminal,
)
from spark_keeper.models import Target

CONVERSATION_ID = "conversation-native"
WATERMARK = "9223372036854775805"
NEXT_INDEX = "9223372036854775806"
RESOURCE_URL = f"https://native-fixture.test{SPARK_STICKER_RESOURCE_PATH}"
ASSET_PATH = Path(__file__).resolve().parents[1] / "src/spark_keeper/assets/spark-sticker.png"


def native_target() -> Target:
    return Target(
        id=1,
        stable_key="native-fixture-friend",
        display_name="测试好友",
        profile_url="https://www.douyin.com/user/native-fixture-friend",
        avatar_url="",
        search_query="测试好友",
        evidence={"identity_strength": "strong", "capture_source": "spark_scan"},
        enabled=True,
        confirmed_at="now",
    )


NATIVE_CHAT_HTML = """
<style>
  .search { position:absolute; left:30px; top:80px; width:230px; height:36px; }
  .pane { position:absolute; left:320px; top:0; width:840px; height:780px; }
  .RightPanelHeaderconvHeader {
    position:absolute; left:40px; top:70px; width:740px; height:90px;
  }
  [data-e2e="msg-input"] {
    position:absolute; left:40px; top:650px; width:740px; height:110px;
  }
  [contenteditable] { width:620px; height:50px; }
  .messageMsgInputiconAction { width:44px; height:32px; }
  .componentsemojiemojiPanel {
    display:none; position:absolute; left:40px; top:350px; width:700px; height:240px;
  }
  .emojiEmojiItememojiItem { display:inline-block; width:100px; height:120px; }
  .emojiEmojiItemimgBox, .emojiEmojiItemimgBox img { width:80px; height:80px; }
  #messages { position:absolute; left:40px; top:190px; width:700px; height:140px; }
  .MessageItemEmojiemojiBox { display:inline-block; width:24px; height:24px; }
  .MessageItemEmojiemojiBox img { width:20px; height:20px; }
</style>
<input class="search" placeholder="搜索已有会话">
<main class="pane">
  <div class="RightPanelHeaderconvHeader">
    <a href="https://www.douyin.com/user/native-fixture-friend"><span>测试好友</span></a>
  </div>
  <div id="messages"></div>
  <div class="componentsemojiim-saas-modal"><div class="semi-modal-content">
  <div class="componentsemojiemojiPanel">
    <div class="emojiEmojiItememojiItem">
      <div class="emojiEmojiItemimgBox" data-apm-action="EmojiItem"><img></div>
      <div class="emojiEmojiItememojiItemDesc">续火花</div>
    </div>
  </div>
  </div></div>
  <div data-e2e="msg-input">
    <div contenteditable="true">保留草稿</div>
    <div class="messageMsgInputinputAction">
      <button class="messageMsgInputiconAction">表情</button>
    </div>
    <button id="send">发送</button>
  </div>
</main>
"""


@asynccontextmanager
async def native_page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})

        async def local_resource(route: Route) -> None:
            if route.request.url.startswith("https://native-fixture.test/"):
                await route.fulfill(path=ASSET_PATH, content_type="image/png")
            else:
                await route.abort()

        try:
            # Every request is intercepted: no live site, SDK or send endpoint exists.
            await page.route("**/*", local_resource)
            await page.set_content(NATIVE_CHAT_HTML)
            await page.evaluate(
                """({url, conversationId, watermark}) => {
                    window.effects = {panel: 0, inputs: 0, sticker: 0, send: 0};
                    window.forbiddenReads = 0;
                    window.poison = object => {
                        for (const key of ['content', 'text', 'ext', 'sender', 'body']) {
                            Object.defineProperty(object, key, {get() {
                                window.forbiddenReads++;
                                throw new Error('unrelated private field read: ' + key);
                            }});
                        }
                        return object;
                    };
                    window.longIndex = value => ({toString() { return value; }});
                    window.conversation = {
                        id: conversationId, type: 1,
                        toParticipantUserId: 'friend-id',
                        firstPageParticipant: {participants: [
                            {user_id: 'friend-id', sec_uid: 'native-fixture-friend'}
                        ]},
                        lastMessageIndexV2: longIndex(watermark),
                        maxIndexV2FromServer: longIndex('9223372036854775804')
                    };
                    window.bind = (element, props) => {
                        element.__reactFiber$nativeFixture = {
                            stateNode: element,
                            memoizedProps: {},
                            return: {stateNode: null, memoizedProps: props, return: null}
                        };
                    };
                    const header = document.querySelector('.RightPanelHeaderconvHeader');
                    const panel = document.querySelector('.componentsemojiemojiPanel');
                    const modal = panel.closest('.componentsemojiim-saas-modal');
                    const item = panel.querySelector('.emojiEmojiItememojiItem');
                    window.sticker = {
                        id: 0, display_name: '续火花', resource_type: 4,
                        animate_type: 'png', static_url: url, animate_url: url
                    };
                    bind(header, {curConversation: conversation});
                    bind(modal, {conversation});
                    bind(panel.parentElement, {});
                    panel.parentElement.__reactFiber$nativeFixture.return =
                        modal.__reactFiber$nativeFixture;
                    bind(panel, {});
                    panel.__reactFiber$nativeFixture.return =
                        panel.parentElement.__reactFiber$nativeFixture;
                    bind(item, {sticker});
                    item.querySelector('img').src = url;
                    document.querySelector('.messageMsgInputiconAction').onclick = () => {
                        effects.panel++;
                        panel.style.display = 'block';
                    };
                    document.querySelector('[contenteditable]').oninput = () => effects.inputs++;
                    panel.addEventListener('click', event => {
                        if (event.target.closest('.emojiEmojiItememojiItem')) effects.sticker++;
                    });
                    document.querySelector('#send').onclick = () => effects.send++;
                    window.addMessage = (changes = {}, options = {}) => {
                        const message = poison({
                            clientId: 'fresh-client', serverId: 'server-1', flightStatus: 3,
                            conversationId, type: 5, isFromMe: true, visible: true,
                            isRefMessage: false, isRecalled: false,
                            indexInConversationV2: longIndex('9223372036854775806'),
                            ...changes
                        });
                        if (options.poisonParsed) {
                            Object.defineProperty(message, 'parsedContent', {get() {
                                forbiddenReads++;
                                throw new Error('unrelated parsed body read');
                            }});
                        } else {
                            message.parsedContent = poison({
                                display_name: '续火花', image_id: 0, aweType: 507,
                                resource_type: 4, url: {uri: url, url_list: [url]},
                                ...options.content
                            });
                        }
                        const box = document.createElement('div');
                        box.className = 'MessageItemEmojiemojiBox';
                        const image = document.createElement('img');
                        image.className = 'commonMyImageimgReal MessageItemEmojiimage';
                        image.src = options.imageUrl || url;
                        box.append(image);
                        document.querySelector('#messages').append(box);
                        // Live MyMessage inherits the SDK Message; it does not copy fields.
                        bind(box, {message: Object.create(message)});
                        return box;
                    };
                }""",
                {"url": RESOURCE_URL, "conversationId": CONVERSATION_ID, "watermark": WATERMARK},
            )
            yield page
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_validation_opens_only_native_panel_and_preserves_composer() -> None:
    async with native_page() as page:
        adapter = DouyinChatAdapter(page)
        await adapter.validate_spark_sticker(native_target())
        # Revalidation of an already-open panel must not toggle or select it.
        await adapter.validate_spark_sticker(native_target())
        assert await page.locator(".componentsemojiemojiPanel").is_visible()
        assert await page.locator("[contenteditable]").inner_text() == "保留草稿"
        assert await page.evaluate("effects") == {"panel": 1, "inputs": 0, "sticker": 0, "send": 0}
        assert await page.evaluate("forbiddenReads") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        (
            "document.querySelector('.componentsemojiemojiPanel').append("
            "document.querySelector('.emojiEmojiItememojiItem').cloneNode(true))"
        ),
        "sticker.static_url += '-wrong-resource'",
        "document.querySelector('.emojiEmojiItemimgBox img').src += '-wrong-resource'",
        "sticker.resource_type = 3",
        (
            "document.querySelector('.componentsemojiim-saas-modal')"
            ".__reactFiber$nativeFixture.return.memoizedProps.conversation = "
            "{...conversation, id: 'another-conversation'}"
        ),
        "conversation.lastMessageIndexV2 = null; conversation.maxIndexV2FromServer = null",
        (
            "conversation.lastMessageIndexV2 = longIndex('0'); "
            "conversation.maxIndexV2FromServer = longIndex('0')"
        ),
    ],
    ids=[
        "duplicate",
        "wrong-prop-resource",
        "wrong-image-resource",
        "wrong-resource-type",
        "wrong-panel-conversation",
        "missing-watermark",
        "zero-watermark",
    ],
)
async def test_native_validation_rejects_unsafe_panel_without_selecting(mutation: str) -> None:
    async with native_page() as page:
        await page.evaluate(f"() => {{ {mutation}; }}")
        with pytest.raises(PageStructureChanged):
            await DouyinChatAdapter(page).validate_spark_sticker(native_target())
        assert await page.evaluate("effects") == {"panel": 1, "inputs": 0, "sticker": 0, "send": 0}
        assert await page.locator("[contenteditable]").inner_text() == "保留草稿"


@pytest.mark.asyncio
async def test_wrong_recipient_is_rejected_before_opening_panel() -> None:
    async with native_page() as page:
        await page.locator(".RightPanelHeaderconvHeader span").evaluate(
            "element => element.textContent = '其他好友'"
        )
        with pytest.raises(TargetIdentityMismatch):
            await DouyinChatAdapter(page).validate_spark_sticker(native_target())
        assert await page.evaluate("effects") == {"panel": 0, "inputs": 0, "sticker": 0, "send": 0}


@pytest.mark.asyncio
async def test_snapshot_whitelists_native_outgoing_messages_without_reading_other_bodies() -> None:
    async with native_page() as page:
        await page.evaluate(
            """url => {
                addMessage();
                addMessage(); // A second mounted view of the same client is not a new send.
                for (const changes of [
                    {type: 7}, {isFromMe: false}, {conversationId: 'other'},
                    {isRefMessage: true}, {isRecalled: true}, {visible: false}
                ]) addMessage(changes, {poisonParsed: true});
                addMessage({}, {imageUrl: url + '-other', poisonParsed: true});
                for (const content of [
                    {aweType: 506}, {image_id: 1}, {resource_type: 3},
                    {url: {uri: url + '-other', url_list: [url]}},
                    {url: {uri: url, url_list: [url + '-other']}}
                ]) addMessage({clientId: 'wrong-' + document.querySelector('#messages').children.length},
                              {content});
            }""",
            RESOURCE_URL,
        )
        snapshot = await DouyinChatAdapter(page)._spark_sticker_snapshot()
        assert snapshot == {
            "conversationId": CONVERSATION_ID,
            "watermark": WATERMARK,
            "messages": [
                {
                    "clientId": "fresh-client",
                    "serverId": "server-1",
                    "flightStatus": 3,
                    "serverHydrated": False,
                    "indexV2": NEXT_INDEX,
                }
            ],
        }
        assert await page.evaluate("forbiddenReads") == 0
        assert await page.evaluate("effects") == {"panel": 0, "inputs": 0, "sticker": 0, "send": 0}


@pytest.mark.asyncio
async def test_snapshot_uses_maximum_long_watermark_and_preserves_missing_message_index() -> None:
    async with native_page() as page:
        await page.evaluate(
            """() => {
                conversation.maxIndexV2FromServer = longIndex('9223372036854775807');
                addMessage({indexInConversationV2: null, flightStatus: 1, serverId: '0'});
            }"""
        )
        snapshot = await DouyinChatAdapter(page)._spark_sticker_snapshot()
        assert snapshot["watermark"] == "9223372036854775807"
        assert snapshot["messages"][0]["indexV2"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,confirmed",
    [
        ("", True),
        ("message.flightStatus = null", False),
        ("message.flightStatus = 0", False),
        ("message.flightStatus = 1", False),
        ("message.flightStatus = 2", False),
        ("message.flightStatus = -1", False),
        ("message.flightStatus = -2", False),
        ("message.flightStatus = -3", False),
        ("message.isOffline = true", False),
        ("delete message.isOffline", False),
        ("message.serverStatus = 1", False),
        ("message.serverId = '0'", False),
        ("message.serverId = 'not-a-server-id'", False),
        ("message.indexInConversationV2 = longIndex('0')", False),
        ("message.indexInConversationV2 = null", False),
    ],
)
async def test_live_server_hydrated_shape_requires_positive_server_proof(
    mutation: str,
    confirmed: bool,
) -> None:
    async with native_page() as page:
        await page.evaluate(
            """mutation => {
                const box = addMessage({flightStatus: undefined, isOffline: false,
                    serverStatus: 0, serverId: '7512345678901234567'});
                const wrapper = box.__reactFiber$nativeFixture.return.memoizedProps.message;
                const message = Object.getPrototypeOf(wrapper);
                eval(mutation);
            }""",
            mutation,
        )
        observed = await DouyinChatAdapter(page)._spark_sticker_snapshot()
        assert observed["messages"][0]["serverHydrated"] is confirmed
        assert await page.evaluate("forbiddenReads") == 0
        assert await page.evaluate("effects") == {"panel": 0, "inputs": 0, "sticker": 0, "send": 0}


@pytest.mark.asyncio
async def test_live_prototype_wrapper_observes_online_self_acknowledgement() -> None:
    async with native_page() as page:
        await page.evaluate("addMessage({flightStatus: 2, serverId: '0'})")
        adapter = DouyinChatAdapter(page)
        before = await adapter._spark_sticker_snapshot()
        assert before["messages"][0]["flightStatus"] == 2
        await page.evaluate(
            """() => {
                const box = document.querySelector('.MessageItemEmojiemojiBox');
                const wrapper = box.__reactFiber$nativeFixture.return.memoizedProps.message;
                Object.assign(Object.getPrototypeOf(wrapper), {
                    flightStatus: 4, serverId: '7512345678901234567', isOffline: false
                });
            }"""
        )
        after = await adapter._spark_sticker_snapshot()
        assert after["messages"][0]["flightStatus"] == 4
        assert after["messages"][0]["serverId"] == "7512345678901234567"
        assert after["messages"][0]["indexV2"] == NEXT_INDEX
        assert await page.evaluate("forbiddenReads") == 0


def message(
    *,
    client_id: str = "new-client",
    status: int | None = 3,
    index: str | None = NEXT_INDEX,
    server_id: str = "server-1",
    server_hydrated: bool = False,
) -> dict[str, Any]:
    return {
        "clientId": client_id,
        "serverId": server_id,
        "flightStatus": status,
        "serverHydrated": server_hydrated,
        "indexV2": index,
    }


def snapshot(*messages: dict[str, Any], conversation_id: str = CONVERSATION_ID) -> dict[str, Any]:
    return {"conversationId": conversation_id, "watermark": WATERMARK, "messages": list(messages)}


class NativeTimeline:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        frames: list[dict[str, Any] | Exception],
        *,
        baseline: dict[str, Any] | None = None,
    ) -> None:
        self.frames = frames
        self.baseline = baseline if baseline is not None else snapshot()
        self.events: list[str] = []
        self.now = 0.0
        self.index = 0
        self.samples = 0
        self.click_error: Exception | None = None
        self.final_identity_error: Exception | None = None
        self.final_panel: dict[str, Any] | Exception = self.baseline
        # No browser can be reached: all adapter boundaries below are local fakes.
        self.adapter = DouyinChatAdapter(None)  # type: ignore[arg-type]
        monkeypatch.setattr(self.adapter, "_prepare_spark_sticker", self.prepare)
        monkeypatch.setattr(self.adapter, "verify_recipient", self.verify)
        monkeypatch.setattr(self.adapter, "_spark_sticker_snapshot", self.take_snapshot)
        monkeypatch.setattr(douyin_chat, "await_delivery_terminal", self.await_terminal)

    async def prepare(self, target: Target) -> tuple[NativeTimeline, dict[str, Any]]:
        assert target == native_target()
        self.events.append("prepare")
        await self.adapter.verify_recipient(target)
        baseline = await self.adapter._spark_sticker_snapshot(require_panel=True)
        return self, baseline

    async def element_handle(self) -> NativeTimeline:
        self.events.append("capture-element")
        return self

    async def dispose(self) -> None:
        self.events.append("dispose")

    async def verify(self, target: Target) -> None:
        assert target == native_target()
        self.events.append("verify")
        if "capture-element" in self.events and self.final_identity_error is not None:
            raise self.final_identity_error

    async def take_snapshot(self, *, require_panel: bool = False) -> dict[str, Any]:
        if require_panel:
            if "capture-element" in self.events:
                self.events.append("recheck-panel")
                if isinstance(self.final_panel, Exception):
                    raise self.final_panel
                return self.final_panel
            self.events.append("baseline")
            return self.baseline
        self.events.append("sample")
        self.samples += 1
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        if isinstance(frame, Exception):
            raise frame
        return frame

    def trigger(self) -> None:
        self.events.append("trigger")

    async def click(self) -> None:
        self.events.append("click")
        if self.click_error is not None:
            raise self.click_error

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.index += 1

    async def await_terminal(
        self, sample: Callable[[], Awaitable[DeliverySample]]
    ) -> DeliveryOutcome:
        self.events.append("await")
        return await await_delivery_terminal(
            sample,
            timeout_seconds=3,
            poll_seconds=0.25,
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    def assert_single_trigger(self) -> None:
        actions = [event for event in self.events if event != "capture-element"]
        assert actions[:8] == [
            "prepare",
            "verify",
            "baseline",
            "verify",
            "recheck-panel",
            "trigger",
            "click",
            "await",
        ]
        assert self.events.count("capture-element") == 1
        assert self.events.index("capture-element") < self.events.index("trigger")
        assert self.events.count("trigger") == 1
        assert self.events.count("click") == 1
        assert self.events.count("baseline") == 1
        assert self.events.count("recheck-panel") == 1
        assert self.events.count("dispose") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,server_hydrated", [(3, False), (4, False), (None, True)])
async def test_first_sample_already_successful_requires_stability_and_one_click(
    monkeypatch: pytest.MonkeyPatch,
    status: int | None,
    server_hydrated: bool,
) -> None:
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(message(status=status, server_hydrated=server_hydrated)),
        ],
    )
    await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()
    assert timeline.samples > 1
    assert timeline.now >= 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_status", [0, 1, 2])
@pytest.mark.parametrize("terminal_status,server_hydrated", [(3, False), (4, False), (None, True)])
async def test_pending_with_new_server_index_binds_client_until_stable_success(
    monkeypatch: pytest.MonkeyPatch,
    pending_status: int,
    terminal_status: int | None,
    server_hydrated: bool,
) -> None:
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(message(status=pending_status, server_id="0")),
            snapshot(message(status=terminal_status, server_hydrated=server_hydrated)),
        ],
    )

    await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()
    assert timeline.now >= 1.0
    assert timeline.samples > 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_status", [-1, -2])
@pytest.mark.parametrize("terminal_status,server_hydrated", [(3, False), (4, False), (None, True)])
async def test_delayed_failure_of_bound_client_wins_over_pending_or_provisional_success(
    monkeypatch: pytest.MonkeyPatch,
    failure_status: int,
    terminal_status: int | None,
    server_hydrated: bool,
) -> None:
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(message(status=1, server_id="0")),
            snapshot(message(status=terminal_status, server_hydrated=server_hydrated)),
            snapshot(message(status=failure_status, server_id="0")),
        ],
    )
    with pytest.raises(SendFailed) as failure:
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    assert failure.value.send_triggered
    timeline.assert_single_trigger()
    assert timeline.now == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate,baseline",
    [
        (message(index=WATERMARK), snapshot()),
        (message(index="9223372036854775804"), snapshot()),
        (
            message(client_id="old-client"),
            snapshot(message(client_id="old-client", index=WATERMARK)),
        ),
        (message(status=None), snapshot()),
        (message(status=-3), snapshot()),
        (message(server_id=""), snapshot()),
        (message(server_id="0"), snapshot()),
        (message(index=None), snapshot()),
        (message(status=-1, index="0", server_id="0"), snapshot()),
        (message(status=-2, index="0", server_id="0"), snapshot()),
    ],
    ids=[
        "equal-index",
        "older-index",
        "old-client-id",
        "missing-status-without-server-proof",
        "self-visible",
        "missing-server",
        "zero-server",
        "missing-index",
        "unbound-failure",
        "unbound-rejection",
    ],
)
async def test_non_terminal_or_stale_evidence_remains_unknown_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    candidate: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    timeline = NativeTimeline(monkeypatch, [snapshot(candidate)], baseline=baseline)
    with pytest.raises(SendUnknown) as unknown:
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    assert unknown.value.send_triggered
    timeline.assert_single_trigger()
    assert timeline.now == 3.0


@pytest.mark.asyncio
async def test_unrelated_failed_client_does_not_fail_bound_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(message(status=1, server_id="0")),
            snapshot(
                message(), message(client_id="unrelated", status=-1, index="0", server_id="0")
            ),
        ],
    )
    await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()


@pytest.mark.asyncio
async def test_multiple_new_candidates_cannot_be_rebound_to_later_single_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(message(), message(client_id="another-client")),
            snapshot(message()),
        ],
    )
    with pytest.raises(SendUnknown):
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "verify", "baseline", "callback"])
async def test_pretrigger_failure_never_clicks_or_samples(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    timeline = NativeTimeline(monkeypatch, [snapshot(message())])
    rejection = RuntimeError("safety gate refused")

    async def reject(*args: Any, **kwargs: Any) -> None:
        raise rejection

    def reject_callback() -> None:
        timeline.events.append("trigger")
        raise rejection

    callback: Callable[[], None] = timeline.trigger
    if stage == "callback":
        callback = reject_callback
    else:
        attribute = {
            "prepare": "_prepare_spark_sticker",
            "verify": "verify_recipient",
            "baseline": "_spark_sticker_snapshot",
        }[stage]
        monkeypatch.setattr(timeline.adapter, attribute, reject)
    with pytest.raises(RuntimeError) as error:
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), callback)
    assert error.value is rejection
    assert "click" not in timeline.events
    assert "await" not in timeline.events
    assert timeline.samples == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["click", "sample", "conversation-change"])
async def test_posttrigger_exceptions_are_unknown_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    frames: list[dict[str, Any] | Exception] = [snapshot(message())]
    if stage == "sample":
        frames = [RuntimeError("snapshot unavailable")]
    elif stage == "conversation-change":
        frames = [snapshot(message(), conversation_id="another-conversation")]
    timeline = NativeTimeline(monkeypatch, frames)
    if stage == "click":
        timeline.click_error = RuntimeError("click acknowledgement lost")
    with pytest.raises(SendUnknown) as unknown:
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    assert unknown.value.send_triggered
    actions = [event for event in timeline.events if event != "capture-element"]
    assert actions[:7] == [
        "prepare",
        "verify",
        "baseline",
        "verify",
        "recheck-panel",
        "trigger",
        "click",
    ]
    assert timeline.events.count("click") == 1
    assert timeline.events.count("trigger") == 1
    assert timeline.samples == (0 if stage == "click" else 1)
    assert timeline.events.count("dispose") == 1


@pytest.mark.asyncio
async def test_unindexed_pending_can_complete_only_when_new_index_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(message(status=1, index="0", server_id="0")),
            snapshot(message()),
        ],
    )
    await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()
    # Unindexed pending did not establish a matching outgoing observation.
    assert timeline.now >= 1.75


@pytest.mark.asyncio
async def test_old_pending_virtual_mount_does_not_capture_new_send_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_pending = message(client_id="old-virtual-mount", status=1, index="0", server_id="0")
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(old_pending),
            snapshot(old_pending, message()),
        ],
    )
    await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_status", [-1, -2])
async def test_unindexed_pending_then_failure_cannot_prove_this_send_failed(
    monkeypatch: pytest.MonkeyPatch,
    failure_status: int,
) -> None:
    timeline = NativeTimeline(
        monkeypatch,
        [
            snapshot(message(status=1, index="0", server_id="0")),
            snapshot(message(status=failure_status, index="0", server_id="0")),
        ],
    )
    with pytest.raises(SendUnknown):
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()
    assert timeline.now == 3.0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "panel-conversation", "panel-unavailable"])
async def test_final_preparation_recipient_or_panel_change_rejects_before_trigger(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    timeline = NativeTimeline(monkeypatch, [snapshot(message())])

    if change == "identity":
        timeline.final_identity_error = TargetIdentityMismatch()
    elif change == "panel-conversation":
        timeline.final_panel = snapshot(conversation_id="other-conversation")
    else:
        timeline.final_panel = PageStructureChanged()

    expected = PageStructureChanged if change == "panel-unavailable" else TargetIdentityMismatch
    with pytest.raises(expected) as rejected:
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    assert rejected.value.send_triggered is False
    assert "trigger" not in timeline.events
    assert timeline.events.count("capture-element") == 1
    assert timeline.events.count("verify") == 2
    assert "click" not in timeline.events
    assert "await" not in timeline.events
    assert timeline.samples == 0
    assert timeline.events.count("dispose") == 1


@pytest.mark.asyncio
async def test_missing_fixed_element_is_rejected_before_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = NativeTimeline(monkeypatch, [snapshot(message())])

    async def missing_element() -> None:
        return None

    monkeypatch.setattr(timeline, "element_handle", missing_element)
    with pytest.raises(PageStructureChanged):
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    assert "trigger" not in timeline.events
    assert "click" not in timeline.events
    assert "await" not in timeline.events
    assert timeline.samples == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status,server_hydrated", [(4, False), (None, True)])
@pytest.mark.parametrize("stale", ["equal-index", "old-client", "preparation-arrival"])
async def test_server_confirmed_history_never_becomes_this_click(
    monkeypatch: pytest.MonkeyPatch,
    status: int | None,
    server_hydrated: bool,
    stale: str,
) -> None:
    candidate = message(
        status=status,
        server_hydrated=server_hydrated,
        index=WATERMARK if stale == "equal-index" else NEXT_INDEX,
    )
    baseline = snapshot(candidate) if stale == "old-client" else snapshot()
    timeline = NativeTimeline(monkeypatch, [snapshot(candidate)], baseline=baseline)
    if stale == "preparation-arrival":
        timeline.final_panel = snapshot(candidate)
    with pytest.raises(SendUnknown):
        await timeline.adapter.send_spark_sticker_and_confirm(native_target(), timeline.trigger)
    timeline.assert_single_trigger()
    assert timeline.now == 3.0


@pytest.mark.asyncio
async def test_identity_change_while_loading_panel_is_rejected_before_trigger() -> None:
    async with native_page() as page:
        adapter = DouyinChatAdapter(page)
        await page.evaluate(
            """() => {
                document.querySelector('.messageMsgInputiconAction').addEventListener('click', () => {
                    document.querySelector('.RightPanelHeaderconvHeader span').textContent = '其他好友';
                });
            }"""
        )
        triggers = []
        with pytest.raises(TargetIdentityMismatch):
            await adapter.send_spark_sticker_and_confirm(
                native_target(), lambda: triggers.append(True)
            )
        assert triggers == []
        assert await page.evaluate("effects") == {"panel": 1, "inputs": 0, "sticker": 0, "send": 0}


@pytest.mark.asyncio
async def test_final_preparation_rechecks_before_trigger_without_clicking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with native_page() as page:
        adapter = DouyinChatAdapter(page)
        readiness = []
        snapshots = []
        original_ready = adapter._require_ready
        original_snapshot = adapter._spark_sticker_snapshot

        async def ready() -> None:
            readiness.append(True)
            await original_ready()

        async def sample(*, require_panel: bool = False) -> dict[str, Any]:
            snapshots.append(require_panel)
            return await original_snapshot(require_panel=require_panel)

        def abort_before_click() -> None:
            assert readiness == [True, True, True]  # Open, load, and final identity check.
            assert snapshots == [True, True]  # Preparation and final pre-trigger baseline.
            raise RuntimeError("stop before any native trigger")

        monkeypatch.setattr(adapter, "_require_ready", ready)
        monkeypatch.setattr(adapter, "_spark_sticker_snapshot", sample)
        with pytest.raises(RuntimeError, match="stop before any native trigger"):
            await adapter.send_spark_sticker_and_confirm(native_target(), abort_before_click)
        assert await page.evaluate("effects") == {"panel": 1, "inputs": 0, "sticker": 0, "send": 0}
