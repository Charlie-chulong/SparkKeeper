from __future__ import annotations

import json
from dataclasses import replace

import pytest
from playwright.async_api import async_playwright

from spark_keeper.automation.douyin_chat import DouyinChatAdapter, PageState
from spark_keeper.automation.errors import TargetAmbiguous, TargetIdentityMismatch, TargetNotFound
from spark_keeper.logging_safe import format_error
from spark_keeper.models import Target


@pytest.mark.asyncio
async def test_page_classifier_distinguishes_login_ready_and_verification() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(
                '<input placeholder="请输入手机号"><div>扫码登录</div><div>密码登录</div>'
            )
            assert await DouyinChatAdapter(page).classify_page() is PageState.LOGIN_REQUIRED

            await page.set_content("<div>请完成安全验证</div>")
            assert await DouyinChatAdapter(page).classify_page() is PageState.HUMAN_VERIFICATION

            await page.set_content('<input placeholder="搜索好友">')
            assert await DouyinChatAdapter(page).classify_page() is PageState.READY
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_safe_diagnostic_does_not_include_page_body_or_query_secret() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto("data:text/html,<title>安全页</title><body>private-chat-message</body>")
            diagnostic = await DouyinChatAdapter(page).safe_diagnostic()
            serialized = repr(diagnostic)
            assert "private-chat-message" not in serialized
            assert "body" not in diagnostic
        finally:
            await browser.close()


def current_chat_html(*, title: str = "测试好友乙", group_suffix: str = "") -> str:
    return f"""
    <style>
      .chat-search {{ position: absolute; left: 40px; top: 80px; width: 240px; height: 36px; }}
      .pane {{ position: absolute; left: 400px; top: 0; width: 760px; height: 780px; }}
      .header {{ position: absolute; left: 50px; top: 70px; width: 650px; height: 80px; }}
      .header img {{ width: 42px; height: 42px; }}
      .composer {{ position: absolute; left: 60px; top: 650px; width: 620px; height: 60px; }}
    </style>
    <input class="chat-search" placeholder="搜索已有会话">
    <main class="pane">
      <div class="header" data-conversation-id="conversation-abc">
        <a href="https://www.douyin.com/user/user-abc">
          <img src="https://example.test/avatar.png">
          <span>{title}</span>
        </a>
        <small>{group_suffix}</small>
      </div>
      <div class="composer" contenteditable="true"></div>
    </main>
    """


def narrow_editor_chat_html(*, title: str = "测试好友乙") -> str:
    return f"""
    <style>
      .chat-search {{ position: absolute; left: 30px; top: 80px; width: 230px; height: 36px; }}
      .conversation {{ position: absolute; left: 30px; top: 140px; width: 230px; height: 60px; }}
      .pane {{ position: absolute; left: 320px; top: 0; width: 840px; height: 780px; }}
      .header {{ position: absolute; left: 40px; top: 70px; width: 740px; height: 90px; }}
      .header img {{ width: 42px; height: 42px; }}
      .composer-shell {{
        position: absolute; left: 40px; top: 650px; width: 760px; height: 72px;
      }}
      .composer {{
        position: absolute; right: 12px; top: 8px; width: 120px; height: 52px;
      }}
    </style>
    <input class="chat-search" placeholder="搜索已有会话">
    <aside class="conversation"><span>测试好友乙</span></aside>
    <main class="pane">
      <div class="header" data-conversation-id="conversation-compact">
        <a href="https://www.douyin.com/user/user-compact">
          <img src="https://example.test/avatar-compact.png">
          <span>{title}</span>
        </a>
      </div>
      <div class="composer-shell">
        <div class="composer" contenteditable="true"></div>
      </div>
    </main>
    """


def conversation_search_html() -> str:
    return """
    <style>
      .left { position: absolute; left: 20px; top: 20px; width: 300px; height: 760px; }
      .chat-search { position: absolute; left: 20px; top: 20px; width: 250px; height: 36px; }
      .search-group {
        position: absolute; left: 0; top: 40px; width: 300px; height: 200px;
      }
      .search-list {
        position: absolute; left: 0; top: 30px; width: 300px; height: 160px;
      }
      .conversation {
        position: absolute; left: 10px; top: 0; width: 280px; height: 72px;
      }
      .group-conversation { top: 76px; }
      .conversation img, .header img { width: 42px; height: 42px; }
      .right { position: absolute; left: 360px; top: 20px; width: 820px; height: 760px; }
      .header { position: absolute; left: 40px; top: 70px; width: 720px; height: 80px; }
      .composer { position: absolute; left: 40px; top: 650px; width: 720px; height: 60px; }
    </style>
    <aside class="left">
      <input class="chat-search" placeholder="搜索已有会话">
      <div class="search-group">
        <div class="search-list">
          <div class="conversation">
            <img src="https://example.test/left-avatar.png">
            <span>测试好友</span>
            <div class="chat-action" style="cursor: pointer" onclick="this.dataset.clicked='true'">
              发消息
            </div>
          </div>
          <div class="conversation group-conversation">
            <img src="https://example.test/group-avatar.png">
            <span>测试好友（7）</span>
            <div class="chat-action" style="cursor: pointer">发消息</div>
          </div>
        </div>
      </div>
    </aside>
    <main class="right">
      <div class="header">
        <img src="https://example.test/right-avatar.png">
        <span>测试好友</span>
      </div>
      <div class="composer" contenteditable="true"></div>
    </main>
    """


@pytest.mark.asyncio
async def test_current_chat_capture_reads_identity_without_touching_composer() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(current_chat_html())
            candidate = await DouyinChatAdapter(page).capture_current_chat_candidate("测试好友乙")
            assert candidate.display_name == "测试好友乙"
            assert candidate.profile_url == "https://www.douyin.com/user/user-abc"
            assert candidate.evidence["capture_source"] == "current_chat"
            assert await page.locator(".composer").inner_text() == ""
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_conversation_search_accepts_pointer_div_and_excludes_right_header() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(conversation_search_html())
            adapter = DouyinChatAdapter(page)
            candidates = await adapter.search_targets("测试好友")
            assert len(candidates) == 1
            assert candidates[0].display_name == "测试好友"
            assert candidates[0].avatar_url == "https://example.test/left-avatar.png"
            diagnostic = adapter._last_candidate_diagnostics
            assert diagnostic["search"]["placeholder"] == "搜索已有会话"
            assert diagnostic["search"]["bounds"]["left"] == 40
            assert diagnostic["returned_rows"] == 1
            assert diagnostic["row_counts"]["group_text"] >= 1
            assert diagnostic["leaf_counts"]["outside_panel"] >= 1
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_open_target_clicks_visible_text_action_without_button_role() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(conversation_search_html())
            adapter = DouyinChatAdapter(page)
            candidate = (await adapter.search_targets("测试好友"))[0]
            target = Target(
                id=1,
                stable_key=candidate.stable_key,
                display_name=candidate.display_name,
                douyin_id=candidate.douyin_id,
                profile_url=candidate.profile_url,
                avatar_url=candidate.avatar_url,
                search_query="测试好友",
                evidence=candidate.evidence,
                enabled=True,
                confirmed_at="now",
            )
            await adapter.open_confirmed_target(target)
            friend_action = page.locator(".conversation:not(.group-conversation) .chat-action")
            group_action = page.locator(".group-conversation .chat-action")
            assert await friend_action.get_attribute("data-clicked") == "true"
            assert await group_action.get_attribute("data-clicked") is None
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_candidates", "expected_matches"),
    [
        ("no_candidates", 0, 0),
        ("identity_mismatch", 1, 0),
        ("row_missing", 1, 1),
        ("row_not_visible", 1, 1),
        ("ambiguous", 2, 2),
    ],
)
async def test_target_lookup_failure_notes_explain_branch_without_opening_or_sending(
    reason: str,
    expected_candidates: int,
    expected_matches: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(conversation_search_html())
            adapter = DouyinChatAdapter(page)
            candidate = adapter._candidate_from_raw(
                (await adapter._extract_candidate_rows("测试好友", page.locator("input")))[0]
            )
            target = Target(
                id=71,
                stable_key=candidate.stable_key,
                display_name=candidate.display_name,
                douyin_id=candidate.douyin_id,
                profile_url=candidate.profile_url,
                avatar_url=candidate.avatar_url,
                search_query="测试好友",
                evidence={**candidate.evidence, "unrelated": "private-evidence-value"},
                enabled=True,
                confirmed_at="now",
            )
            if reason == "no_candidates":
                target = replace(target, search_query="不存在的好友")
            elif reason == "identity_mismatch":
                target = replace(
                    target,
                    stable_key="different-saved-identity",
                    profile_url="https://www.douyin.com/user/saved-user",
                )
            elif reason == "ambiguous":
                await page.locator(".group-conversation").evaluate(
                    """group => {
                        const copy = document.querySelector(
                            '.conversation:not(.group-conversation)'
                        ).cloneNode(true);
                        copy.style.top = '76px';
                        group.replaceWith(copy);
                    }"""
                )
            else:
                extract = adapter._extract_candidate_rows

                async def extract_then_invalidate(query, search):
                    rows = await extract(query, search)
                    row = page.locator(f'[data-sk-candidate-token="{rows[0]["token"]}"]')
                    if reason == "row_missing":
                        await row.evaluate("row => row.remove()")
                    else:
                        await row.evaluate("row => row.style.display = 'none'")
                    return rows

                monkeypatch.setattr(adapter, "_extract_candidate_rows", extract_then_invalidate)

            async def forbidden_send(*_args, **_kwargs):
                pytest.fail("Lookup failure must not reach sending")

            monkeypatch.setattr(adapter, "send_text_and_confirm", forbidden_send)
            await page.locator(".composer").fill("private-chat-message")
            await page.evaluate(
                """() => {
                    window.targetClicks = 0;
                    document.addEventListener('click', event => {
                        if (event.target.closest('.conversation, .composer')) {
                            window.targetClicks++;
                        }
                    });
                }"""
            )
            expected_error = TargetAmbiguous if reason == "ambiguous" else TargetNotFound
            with pytest.raises(expected_error) as caught:
                await adapter.open_confirmed_target(target)

            diagnostic = json.loads(caught.value.__notes__[0])
            assert diagnostic["reason"] == reason
            assert diagnostic["target"]["id"] == 71
            assert diagnostic["target"]["display_name"] == "测试好友"
            assert diagnostic["target"]["stable_key"] == target.stable_key
            assert diagnostic["target"]["search_query"] == target.search_query
            assert diagnostic["candidate_count"] == expected_candidates
            assert diagnostic["match_count"] == expected_matches
            assert diagnostic["page_url"] == "about:blank"
            assert diagnostic["page_state_before_search"] == "ready"
            extraction = diagnostic["extraction"]
            assert extraction["search"]["placeholder"] == "搜索已有会话"
            assert extraction["search"]["bounds"]["left"] == 40
            assert extraction["search"]["value"] == target.search_query
            assert extraction["search"]["focused"] is True
            assert extraction["search"]["disabled"] is False
            assert extraction["document_state"] == "complete"
            assert extraction["returned_rows"] == expected_candidates
            if reason == "no_candidates":
                assert extraction["leaf_counts"]["query_mismatch"] > 0
                assert extraction["poll_count"] == 20
                assert extraction["poll_wait_ms"] == 5000
                assert extraction["query_submissions"] == 2
                assert extraction["total_poll_wait_ms"] == 10000
                previous = extraction["previous_search_attempts"]
                assert len(previous) == 1
                assert previous[0]["query_submissions"] == 1
                assert previous[0]["returned_rows"] == 0
                assert previous[0]["poll_wait_ms"] == 5000
                assert previous[0]["previous_search_attempts"] == []
            else:
                assert extraction["query_submissions"] == 1
                assert extraction["previous_search_attempts"] == []
                assert diagnostic["candidates"][0]["display_name"] == "测试好友"
                assert diagnostic["candidates"][0]["stable_key"] == candidate.stable_key
                assert diagnostic["candidates"][0]["matches_saved_identity"] is (
                    reason != "identity_mismatch"
                )
            detail = format_error(caught.value)
            assert reason in detail
            assert "测试好友" in detail
            assert "private-chat-message" not in detail
            assert "private-evidence-value" not in detail
            assert await page.evaluate("window.targetClicks") == 0
            assert await page.locator(".composer").inner_text() == "private-chat-message"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_current_chat_capture_uses_composer_panel_not_editor_width() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(narrow_editor_chat_html())
            candidate = await DouyinChatAdapter(page).capture_current_chat_candidate("测试好友乙")
            assert candidate.display_name == "测试好友乙"
            assert candidate.profile_url == "https://www.douyin.com/user/user-compact"
            assert await page.locator(".composer").inner_text() == ""

            await page.set_content(narrow_editor_chat_html(title="其他好友"))
            with pytest.raises(TargetIdentityMismatch):
                await DouyinChatAdapter(page).capture_current_chat_candidate("测试好友乙")
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_current_chat_capture_rejects_wrong_title_and_group_chat() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(current_chat_html())
            with pytest.raises(TargetIdentityMismatch) as mismatch:
                await DouyinChatAdapter(page).capture_current_chat_candidate("其他好友")
            assert "右侧聊天顶部未显示与输入完全相同的名称" in str(mismatch.value)

            for group_suffix in ("3 人", "（7）"):
                await page.set_content(current_chat_html(group_suffix=group_suffix))
                with pytest.raises(TargetAmbiguous):
                    await DouyinChatAdapter(page).capture_current_chat_candidate("测试好友乙")

            await page.set_content(current_chat_html(title="测试好友乙（7）"))
            with pytest.raises(TargetAmbiguous):
                await DouyinChatAdapter(page).capture_current_chat_candidate("测试好友乙（7）")
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ready_on_submission", [1, 2])
async def test_unicode_search_submits_keyup_and_waits_for_delayed_results(
    ready_on_submission: int,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(conversation_search_html())
            await page.evaluate(
                """readyOnSubmission => {
                    const results = document.querySelector('.search-group');
                    results.style.display = 'none';
                    window.queryCommits = [];
                    window.enterCount = 0;
                    document.addEventListener('keyup', event => {
                        const input = event.target;
                        if (!input.matches('input')) return;
                        if (event.key === 'Enter') window.enterCount++;
                        if (event.key === 'Backspace' && input.value === '测试好友') {
                            window.queryCommits.push(input.value);
                            if (window.queryCommits.length === readyOnSubmission) {
                                setTimeout(() => { results.style.display = ''; }, 1600);
                            } else {
                                const replacement = input.cloneNode(true);
                                replacement.placeholder = '搜索好友（新输入）';
                                input.replaceWith(replacement);
                            }
                        }
                    });
                }""",
                ready_on_submission,
            )
            adapter = DouyinChatAdapter(page)
            candidates = await adapter.search_targets("测试好友")
            assert len(candidates) == 1
            assert candidates[0].display_name == "测试好友"
            assert await page.locator("input").input_value() == "测试好友"
            assert await page.evaluate("window.queryCommits") == ["测试好友"] * ready_on_submission
            assert await page.evaluate("window.enterCount") == 0
            diagnostic = adapter._last_candidate_diagnostics
            assert diagnostic["poll_count"] > 1
            assert diagnostic["query_submissions"] == ready_on_submission
            assert diagnostic["total_poll_wait_ms"] == (
                (ready_on_submission - 1) * 5000 + diagnostic["poll_wait_ms"]
            )
            if ready_on_submission == 2:
                previous = diagnostic["previous_search_attempts"]
                assert len(previous) == 1
                assert previous[0]["returned_rows"] == 0
                assert previous[0]["poll_count"] == 20
                assert previous[0]["poll_wait_ms"] == 5000
                assert diagnostic["search"]["placeholder"] == "搜索好友（新输入）"
            else:
                assert diagnostic["previous_search_attempts"] == []
            assert json.loads(json.dumps(diagnostic, ensure_ascii=False)) == diagnostic
            assert await page.locator(".chat-action[data-clicked]").count() == 0
            assert await page.locator(".composer").inner_text() == ""
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_search_item_conversation_type_rejects_group_without_name_marker() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        try:
            await page.set_content(conversation_search_html())
            await page.locator(".group-conversation").evaluate("row => row.remove()")
            await page.locator(".conversation").evaluate(
                """row => {
                    row.__reactFiber$fixture = {
                        memoizedProps: {item: {conversation: {
                            id: 'group-conversation',
                            type: 2,
                            get toParticipantUserId() { throw Error('Not a private chat'); },
                            get toParticipantSecUserId() { throw Error('No getter allowed'); }
                        }}},
                        return: null
                    };
                }"""
            )
            adapter = DouyinChatAdapter(page)
            rows = await adapter._extract_candidate_rows("测试好友", page.locator("input"))
            assert rows == []
            assert (
                adapter._last_candidate_diagnostics["row_counts"]["non_private_conversation"] >= 1
            )
            assert await page.locator(".chat-action[data-clicked]").count() == 0
            assert await page.locator(".composer").inner_text() == ""
        finally:
            await browser.close()
