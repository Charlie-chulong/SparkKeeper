from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from ..dpapi import DpapiJsonStore, SecretDataError
from ..models import ErrorCode
from .errors import AutomationError


@dataclass(slots=True)
class BrowserSession:
    page: Page
    context: BrowserContext


def _configure_bundled_browser_path() -> None:
    if not getattr(sys, "frozen", False):
        return
    bundled_browsers = Path(sys.executable).resolve().parent / "browsers"
    if not bundled_browsers.is_dir():
        raise AutomationError(
            ErrorCode.CONFIGURATION_INVALID,
            "程序包缺少 Chromium 组件，请重新解压完整便携包",
            fatal=True,
        )
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled_browsers)


class BrowserSessionFactory:
    def __init__(self, state_store: DpapiJsonStore) -> None:
        self.state_store = state_store

    @asynccontextmanager
    async def open(
        self,
        *,
        headless: bool,
        use_saved_state: bool,
    ) -> AsyncIterator[BrowserSession]:
        playwright: Playwright | None = None
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            _configure_bundled_browser_path()
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=headless)
            context_options: dict[str, object] = {
                "locale": "zh-CN",
                "viewport": {"width": 1440, "height": 1000},
                "accept_downloads": False,
            }
            if use_saved_state:
                try:
                    context_options["storage_state"] = self.state_store.load()
                except SecretDataError as exc:
                    raise AutomationError(
                        ErrorCode.LOGIN_STATE_UNAVAILABLE,
                        "无法读取本机登录状态，请重新扫码",
                        fatal=True,
                    ) from exc
            context = await browser.new_context(**context_options)
            context.set_default_timeout(12_000)
            context.set_default_navigation_timeout(45_000)
            page = await context.new_page()
            yield BrowserSession(page=page, context=context)
        finally:
            async def close_session() -> None:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    try:
                        if browser is not None:
                            await browser.close()
                    finally:
                        if playwright is not None:
                            await playwright.stop()

            cleanup = asyncio.create_task(close_session())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
