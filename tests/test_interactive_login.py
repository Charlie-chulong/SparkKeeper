from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from spark_keeper.automation import browser as browser_module
from spark_keeper.automation.browser import BrowserSessionFactory
from spark_keeper.automation.douyin_chat import (
    DouyinChatAdapter,
    LoginResult,
    PageState,
    derive_account_identity,
    interactive_login,
)
from spark_keeper.automation.errors import AutomationError
from spark_keeper.models import ErrorCode


class LoginFactory:
    def __init__(self, *, block: str = "", profile: str = "https://www.douyin.com/user/account-one") -> None:
        self.block = block
        self.profile = profile
        self.entered = asyncio.Event()
        self.events: list[str] = []
        self.state = {"cookies": [{"name": "sid_tt", "value": "private-session-secret"}]}
        self.session = SimpleNamespace(page=self, context=self)

    async def stage(self, name: str) -> None:
        self.events.append(name)
        if self.block == name:
            self.entered.set()
            await asyncio.Future()

    @asynccontextmanager
    async def open(self, *, headless: bool, use_saved_state: bool):
        assert headless is False and use_saved_state is False
        try:
            await self.stage("open")
            yield self.session
        finally:
            await asyncio.sleep(0)
            self.events.append("closed")

    async def goto(self, *args, **kwargs) -> None:
        await self.stage("navigate")

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, expression: str):
        if "const mine" in expression:
            await self.stage("identity")
            return self.profile
        await self.stage("poll")
        return PageState.READY.value

    async def storage_state(self):
        await self.stage("state")
        return self.state


@pytest.mark.asyncio
async def test_login_returns_only_memory_state_after_browser_closed() -> None:
    factory = LoginFactory()
    statuses: list[str] = []
    result = await interactive_login(factory, status=statuses.append)
    assert isinstance(result, LoginResult)
    assert result.storage_state is factory.state
    assert factory.events == ["open", "navigate", "poll", "state", "identity", "closed"]
    assert statuses == ["登录成功，正在读取账号状态"]
    assert "private-session-secret" not in repr(result)
    assert "private-session-secret" not in repr([result, result.account])
    other = LoginFactory(profile="https://www.douyin.com/user/account-two")
    other.state = {"cookies": [{"name": "sid_tt", "value": "other-secret"}]}
    second = await interactive_login(other)
    assert result.account.platform_user_id != second.account.platform_user_id
    assert result.storage_state is not second.storage_state
    assert "other-secret" not in repr(second)
    assert result.storage_state["cookies"][0]["value"] == "private-session-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["open", "navigate", "poll", "state", "identity"])
async def test_login_cancellation_interrupts_wait_and_awaits_cleanup(stage: str) -> None:
    factory = LoginFactory(block=stage)
    cancel = threading.Event()
    pending = asyncio.create_task(interactive_login(factory, cancel=cancel))
    await asyncio.wait_for(factory.entered.wait(), timeout=2)
    cancel.set()
    with pytest.raises(AutomationError) as caught:
        await asyncio.wait_for(pending, timeout=2)
    assert caught.value.code is ErrorCode.CANCELLED
    assert caught.value.safe_message == "登录已取消"
    assert factory.events[-1] == "closed"
    if stage in {"open", "navigate", "poll"}:
        assert "state" not in factory.events


@pytest.mark.asyncio
async def test_precancelled_login_never_opens_browser() -> None:
    factory = LoginFactory()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AutomationError) as caught:
        await interactive_login(factory, cancel=cancel)
    assert caught.value.code is ErrorCode.CANCELLED
    assert factory.events == []


@pytest.mark.asyncio
async def test_adapter_login_navigation_can_be_cancelled_directly() -> None:
    factory = LoginFactory(block="navigate")
    cancel = threading.Event()
    pending = asyncio.create_task(DouyinChatAdapter(factory).wait_for_interactive_login(cancel=cancel))
    await asyncio.wait_for(factory.entered.wait(), timeout=2)
    cancel.set()
    with pytest.raises(AutomationError) as caught:
        await asyncio.wait_for(pending, timeout=2)
    assert caught.value.code is ErrorCode.CANCELLED
    assert factory.events == ["navigate"]


@pytest.mark.asyncio
async def test_session_only_identity_is_rejected_without_returning_credentials() -> None:
    factory = LoginFactory(profile="")
    with pytest.raises(AutomationError) as caught:
        await interactive_login(factory)
    assert caught.value.code is ErrorCode.CONFIGURATION_INVALID
    assert caught.value.fatal is True
    assert factory.events[-1] == "closed"
    assert "private-session-secret" not in repr(caught.value)


@pytest.mark.asyncio
async def test_stable_identity_does_not_change_with_session_cookie() -> None:
    factory = LoginFactory(profile="")
    factory.state["cookies"].append({"name": "uid_tt", "value": "stable-account-id"})
    first = await derive_account_identity(factory.session)
    factory.state["cookies"][0]["value"] = "rotated-session-secret"
    second = await derive_account_identity(factory.session)
    assert first.platform_user_id == second.platform_user_id
    assert "stable-account-id" not in repr(first)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_close", [False, True])
async def test_browser_factory_finishes_all_cleanup(
    monkeypatch: pytest.MonkeyPatch, cancel_during_close: bool
) -> None:
    events: list[str] = []
    closing = asyncio.Event()
    release = asyncio.Event()
    cancel = threading.Event()
    login = LoginFactory()

    class Context:
        def set_default_timeout(self, value): pass
        def set_default_navigation_timeout(self, value): pass

        async def new_page(self):
            return login

        async def storage_state(self):
            return login.state

        async def close(self):
            events.append("context-closing")
            closing.set()
            if cancel_during_close:
                await release.wait()
                events.append("context-closed")
            else:
                raise RuntimeError("context-close-error")

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

        async def close(self):
            events.append("browser-closed")

    class Driver:
        @property
        def chromium(self):
            return self

        async def start(self):
            return self

        async def launch(self, **kwargs):
            return Browser()

        async def stop(self):
            events.append("driver-stopped")

    monkeypatch.setattr(browser_module, "async_playwright", Driver)
    factory = BrowserSessionFactory(SimpleNamespace())
    pending = asyncio.create_task(interactive_login(factory, cancel=cancel))
    await asyncio.wait_for(closing.wait(), timeout=2)
    if cancel_during_close:
        cancel.set()
        # The login cancellation loop polls at 100 ms. Cleanup must remain alive
        # while this context deliberately delays its normal close.
        await asyncio.sleep(0.2)
        assert not pending.done()
        release.set()
        with pytest.raises(AutomationError) as caught:
            await asyncio.wait_for(pending, timeout=2)
        assert caught.value.code is ErrorCode.CANCELLED
        assert events == ["context-closing", "context-closed", "browser-closed", "driver-stopped"]
    else:
        with pytest.raises(RuntimeError, match="context-close-error"):
            await asyncio.wait_for(pending, timeout=2)
        assert events == ["context-closing", "browser-closed", "driver-stopped"]
