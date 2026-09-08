from __future__ import annotations

import os

import pytest
from playwright.async_api import async_playwright

from spark_keeper.automation.douyin_chat import CHAT_URL, DouyinChatAdapter, PageState


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("SPARK_KEEPER_LIVE_TEST") != "1", reason="显式在线验证时运行")
@pytest.mark.asyncio
async def test_current_douyin_chat_login_surface_is_recognized() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(locale="zh-CN", viewport={"width": 1440, "height": 1000})
        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
            adapter = DouyinChatAdapter(page)
            state = PageState.NOT_READY
            for _ in range(40):
                state = await adapter.classify_page()
                if state is not PageState.NOT_READY:
                    break
                await page.wait_for_timeout(250)
            assert state is PageState.LOGIN_REQUIRED
            diagnostic = await adapter.safe_diagnostic()
            assert diagnostic["url"] == CHAT_URL
            assert "cookies" not in repr(diagnostic).casefold()
        finally:
            await browser.close()
