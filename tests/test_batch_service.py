from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from spark_keeper.automation.errors import AuthenticationRequired, SendFailed, TargetNotFound
from spark_keeper.automation.service import BatchService, wait_for_delay
from spark_keeper.database import Database, today_iso
from spark_keeper.dpapi import DpapiJsonStore
from spark_keeper.logging_safe import target_label
from spark_keeper.models import (
    Account,
    AttemptStatus,
    BatchMode,
    BatchStatus,
    ErrorCode,
    FriendCandidate,
    MessageKind,
)


class FakeFactory:
    @asynccontextmanager
    async def open(self, **_):
        yield SimpleNamespace(page=object())


class ContinuingAdapter:
    def __init__(self, _page) -> None:
        pass

    async def open_chat(self) -> None:
        pass

    async def open_confirmed_target(self, target) -> None:
        if target.display_name == "失败好友":
            raise TargetNotFound()

    async def composer_available(self) -> bool:
        return True

    async def verify_recipient(self, _target) -> None:
        pass

    async def send_text_and_confirm(self, _text, on_trigger) -> None:
        on_trigger()


class FatalAdapter(ContinuingAdapter):
    async def open_confirmed_target(self, _target) -> None:
        raise AuthenticationRequired()


class CaptureAdapter:
    def __init__(self, _page) -> None:
        pass

    async def open_chat(self) -> None:
        pass

    async def capture_current_chat_candidate(self, expected_name: str) -> FriendCandidate:
        return FriendCandidate(
            "captured-key",
            expected_name,
            profile_url="https://www.douyin.com/user/captured",
            evidence={"capture_source": "current_chat"},
        )


def setup_service(tmp_path) -> tuple[BatchService, Database]:
    database = Database(tmp_path / "state.sqlite3")
    database.save_account("account-key", "测试账号")
    database.add_target(FriendCandidate("friend-1", "失败好友", douyin_id="id-1"), "失败")
    database.add_target(FriendCandidate("friend-2", "成功好友", douyin_id="id-2"), "成功")
    database.save_plan(enabled=False, send_time="09:00", message_text="测试消息", confirmed=True)
    state = DpapiJsonStore(tmp_path / "auth-state.bin")
    state.path.write_bytes(b"exists-for-fake-browser")
    service = BatchService(database, state, delay_sampler=lambda _minimum, _maximum: 0.0)
    service.browser_factory = FakeFactory()
    return service, database


@pytest.mark.asyncio
async def test_native_batch_shares_text_guard_and_manual_override_audit(tmp_path, monkeypatch) -> None:
    service, database = setup_service(tmp_path)
    target = database.list_targets()[1]
    sent = []

    class BothKindsAdapter(ContinuingAdapter):
        async def send_text_and_confirm(self, text, on_trigger) -> None:
            sent.append(MessageKind.TEXT)
            on_trigger()

        async def send_spark_sticker_and_confirm(self, selected, on_trigger) -> None:
            assert selected == target
            sent.append(MessageKind.SPARK_STICKER)
            on_trigger()

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", BothKindsAdapter)
    first = await service.run_batch(BatchMode.MANUAL, target_ids=[target.id])
    database.save_plan(
        enabled=False, send_time="09:00", message_text="", confirmed=True,
        message_kind=MessageKind.SPARK_STICKER,
    )
    duplicate = await service.run_batch(BatchMode.SCHEDULED, target_ids=[target.id])
    assert duplicate.results[0].status is AttemptStatus.DUPLICATE
    assert sent == [MessageKind.TEXT]
    with pytest.raises(ValueError, match="定时任务不能覆盖"):
        await service.run_batch(
            BatchMode.SCHEDULED, target_ids=[target.id], manual_override_target_ids=[target.id],
        )
    override = await service.run_batch(
        BatchMode.MANUAL, target_ids=[target.id], manual_override_target_ids=[target.id],
    )
    assert override.results[0].status is AttemptStatus.SUCCESS
    assert sent == [MessageKind.TEXT, MessageKind.SPARK_STICKER]
    row = database.get_attempt(override.results[0].attempt_id)
    assert row["manual_override"] == 1
    assert row["override_of"] == first.results[0].attempt_id
    assert row["message_kind"] == "spark_sticker"


class StickerAdapter(ContinuingAdapter):
    async def verify_recipient(self, _target) -> None:
        raise AssertionError("原生方法负责准备与点击前核验，service不能重复核验")

    async def composer_available(self) -> bool:
        raise AssertionError("原生表情不依赖文本输入框")

    async def send_text_and_confirm(self, _text, on_trigger) -> None:
        raise AssertionError("原生表情不能降级为文本")

    async def validate_spark_sticker(self, target) -> None:
        assert target.stable_key

    async def send_spark_sticker_and_confirm(self, target, on_trigger) -> None:
        assert target.stable_key
        on_trigger()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_error", [TargetNotFound, RuntimeError])
async def test_native_dispatch_records_failed_and_triggered_history(
    tmp_path, monkeypatch, lookup_error
) -> None:
    service, database = setup_service(tmp_path)
    database.save_plan(
        enabled=False, send_time="09:00", message_text="", confirmed=True,
        message_kind=MessageKind.SPARK_STICKER,
    )
    sends = []

    class RecordingStickerAdapter(StickerAdapter):
        async def open_confirmed_target(self, target) -> None:
            if target.display_name == "失败好友":
                raise lookup_error()

        async def send_spark_sticker_and_confirm(self, target, on_trigger) -> None:
            row = next(row for row in database.list_history() if row["status"] == "sending")
            assert row["message_kind"] == "spark_sticker"
            assert database.get_attempt(row["id"])["triggered_at"] is None
            await super().send_spark_sticker_and_confirm(target, on_trigger)
            assert database.get_attempt(row["id"])["triggered_at"]
            sends.append(target.id)

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", RecordingStickerAdapter)
    result = await service.run_batch(BatchMode.MANUAL)
    assert [item.status for item in result.results] == [AttemptStatus.FAILED, AttemptStatus.SUCCESS]
    assert sends == [database.list_targets()[1].id]
    for row in database.list_history():
        assert row["message_kind"] == "spark_sticker"
        assert row["message_text"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_native_validation_uses_snapshot_without_reserving_or_triggering(
    tmp_path, monkeypatch, available
) -> None:
    service, database = setup_service(tmp_path)
    previous = database.get_plan()
    checked = []

    class ValidationStickerAdapter(StickerAdapter):
        async def open_confirmed_target(self, _target) -> None:
            pass

        async def validate_spark_sticker(self, target) -> None:
            checked.append(target.id)
            if not available:
                raise TargetNotFound()

        async def send_spark_sticker_and_confirm(self, target, on_trigger) -> None:
            raise AssertionError("验证不能进入发送路径")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("验证不能预留、记录发送或标记触发")

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", ValidationStickerAdapter)
    monkeypatch.setattr(database, "reserve_attempt", forbidden)
    monkeypatch.setattr(database, "record_terminal_attempt", forbidden)
    monkeypatch.setattr(database, "mark_attempt_triggered", forbidden)
    result = await service.validate_config(
        message_text_override="", message_kind_override=MessageKind.SPARK_STICKER,
    )
    expected = AttemptStatus.SUCCESS if available else AttemptStatus.FAILED
    assert [item.status for item in result.results] == [expected, expected]
    assert checked == [target.id for target in database.list_targets()]
    assert database.count_rows("send_attempts") == database.count_rows("daily_send_guards") == 0
    assert database.get_plan() == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_kind", list(MessageKind))
async def test_missed_message_snapshot_is_independent_of_current_plan(
    tmp_path, monkeypatch, snapshot_kind
) -> None:
    service, database = setup_service(tmp_path)
    current_kind = (
        MessageKind.SPARK_STICKER if snapshot_kind is MessageKind.TEXT else MessageKind.TEXT
    )
    database.save_plan(
        enabled=False, send_time="09:00", message_text="当前计划", confirmed=True,
        message_kind=current_kind,
    )
    sent = []
    target = database.list_targets()[1]

    class SnapshotAdapter(ContinuingAdapter):
        async def send_text_and_confirm(self, text, on_trigger) -> None:
            sent.append((MessageKind.TEXT, text))
            on_trigger()

        async def send_spark_sticker_and_confirm(self, selected, on_trigger) -> None:
            assert selected == target
            sent.append((MessageKind.SPARK_STICKER, ""))
            on_trigger()

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", SnapshotAdapter)
    text = "旧快照正文" if snapshot_kind is MessageKind.TEXT else ""
    result = await service.run_batch(
        BatchMode.MISSED, target_ids=[target.id], message_text_override=text,
        message_kind_override=snapshot_kind,
    )
    assert result.status is BatchStatus.SUCCESS
    assert sent == [(snapshot_kind, text)]
    history = database.list_history()[0]
    assert (history["message_kind"], history["message_text"]) == (snapshot_kind.value, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"message_text_override": "旧正文"},
    {"message_kind_override": MessageKind.SPARK_STICKER},
    {"message_kind_override": "unsupported", "message_text_override": ""},
    {"message_kind_override": MessageKind.TEXT, "message_text_override": " "},
])
async def test_invalid_message_snapshot_is_rejected_before_batch(tmp_path, overrides) -> None:
    service, database = setup_service(tmp_path)
    with pytest.raises(ValueError):
        await service.run_batch(BatchMode.MANUAL, **overrides)
    assert database.count_rows("batch_runs") == database.count_rows("send_attempts") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after", "domain_after", "explicit_failure"])
async def test_native_failures_preserve_triggered_unknown_guard(
    tmp_path, monkeypatch, phase
) -> None:
    service, database = setup_service(tmp_path)
    target = database.list_targets()[1]

    class FailingStickerAdapter(StickerAdapter):
        async def send_spark_sticker_and_confirm(self, target, on_trigger) -> None:
            if phase != "before":
                on_trigger()
            if phase == "domain_after":
                raise TargetNotFound()
            if phase == "explicit_failure":
                raise SendFailed()
            raise RuntimeError("表情页面操作中断")

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", FailingStickerAdapter)
    result = await service.run_batch(
        BatchMode.MANUAL, target_ids=[target.id],
        message_kind_override=MessageKind.SPARK_STICKER, message_text_override="",
    )
    unknown = phase in {"after", "domain_after"}
    expected = AttemptStatus.UNKNOWN if unknown else AttemptStatus.FAILED
    item = result.results[0]
    assert item.status is expected
    assert database.has_daily_guard("account-key", target.id, today_iso()) == unknown
    row = database.get_attempt(item.attempt_id)
    assert row["status"] == expected.value
    assert row["message_kind"] == "spark_sticker"
    assert bool(row["triggered_at"]) == (phase != "before")
    assert bool(database.list_pending_actions("unknown_send")) == unknown
    if unknown:
        assert item.error_code is ErrorCode.SEND_UNKNOWN


@pytest.mark.asyncio
async def test_native_cancel_during_delay_never_triggers_remaining_target(
    tmp_path, monkeypatch
) -> None:
    service, database = setup_service(tmp_path)
    cancel = threading.Event()
    sends = []
    waited = []

    class SendingStickerAdapter(StickerAdapter):
        async def open_confirmed_target(self, _target) -> None:
            pass

        async def send_spark_sticker_and_confirm(self, target, on_trigger) -> None:
            on_trigger()
            sends.append(target.id)

    async def cancel_wait(seconds, event):
        waited.append(seconds)
        event.set()
        return True

    service.delay_sampler = lambda minimum, maximum: (minimum + maximum) / 2
    service.delay_waiter = cancel_wait
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", SendingStickerAdapter)
    result = await service.run_batch(
        BatchMode.MANUAL, message_kind_override=MessageKind.SPARK_STICKER,
        message_text_override="", inter_target_delay_override=(4, 8), cancel=cancel,
    )
    assert waited == [6]
    assert sends == [database.list_targets()[0].id]
    assert [item.status for item in result.results] == [
        AttemptStatus.SUCCESS, AttemptStatus.CANCELLED,
    ]
    assert database.count_rows("send_attempts") == 1


@pytest.mark.asyncio
async def test_default_delay_waiter_stops_immediately_when_cancelled() -> None:
    cancel = threading.Event()
    cancel.set()

    assert await wait_for_delay(30, cancel)


@pytest.mark.asyncio
async def test_target_failure_continues_to_next_target(tmp_path, monkeypatch) -> None:
    service, database = setup_service(tmp_path)
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", ContinuingAdapter)
    result = await service.run_batch(BatchMode.MANUAL)
    assert result.status is BatchStatus.PARTIAL
    assert [item.status for item in result.results] == [AttemptStatus.FAILED, AttemptStatus.SUCCESS]
    assert [item.target_alias for item in result.results] == [
        target_label(target) for target in database.list_targets()
    ]
    target_events = [event for event in database.list_events() if event["target_alias"]]
    assert {event["target_alias"] for event in target_events} == {
        target_label(target) for target in database.list_targets()
    }


@pytest.mark.asyncio
async def test_batch_sends_all_enabled_targets_beyond_five_in_order(tmp_path, monkeypatch) -> None:
    service, database = setup_service(tmp_path)
    disabled, first_enabled = database.list_targets()
    database.set_target_enabled(disabled.id, False)
    targets = [first_enabled]
    targets.extend(
        database.add_target(
            FriendCandidate(f"friend-{index}", f"好友{index}", douyin_id=f"id-{index}"),
            f"好友{index}",
        )
        for index in range(3, 9)
    )
    calls: list[tuple[str, int]] = []

    class RecordingAdapter(ContinuingAdapter):
        async def open_confirmed_target(self, target) -> None:
            self.target_id = target.id
            calls.append(("open", target.id))
            await super().open_confirmed_target(target)

        async def verify_recipient(self, target) -> None:
            calls.append(("verify", target.id))
            await super().verify_recipient(target)

        async def send_text_and_confirm(self, text, on_trigger) -> None:
            target_id = self.target_id
            await super().send_text_and_confirm(text, on_trigger)
            await asyncio.sleep(0)
            calls.append(("sent", target_id))

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", RecordingAdapter)

    result = await service.run_batch(BatchMode.MANUAL)

    assert result.status is BatchStatus.SUCCESS
    assert [item.target_id for item in result.results] == [target.id for target in targets]
    assert [item.status for item in result.results] == [AttemptStatus.SUCCESS] * 7
    assert calls == [
        (operation, target.id) for target in targets for operation in ("open", "verify", "sent")
    ]
    assert all(
        database.has_daily_guard("account-key", target.id, today_iso()) for target in targets
    )
    assert not database.has_daily_guard("account-key", disabled.id, today_iso())


@pytest.mark.asyncio
async def test_batch_waits_randomized_interval_only_between_actual_sends(
    tmp_path, monkeypatch
) -> None:
    service, database = setup_service(tmp_path)
    disabled, first_enabled = database.list_targets()
    database.set_target_enabled(disabled.id, False)
    second_enabled = database.add_target(
        FriendCandidate("friend-3", "另一位好友", douyin_id="id-3"),
        "另一位好友",
    )
    database.save_plan(
        enabled=False,
        send_time="09:00",
        message_text="测试消息",
        confirmed=True,
        delay_min_seconds=4,
        delay_max_seconds=9,
    )
    sampled_ranges: list[tuple[float, float]] = []
    waited_seconds: list[float] = []
    progress: list[tuple[str, str]] = []

    def sample_delay(minimum: float, maximum: float) -> float:
        sampled_ranges.append((minimum, maximum))
        return 6.5

    async def record_wait(seconds: float, _cancel: threading.Event | None) -> bool:
        waited_seconds.append(seconds)
        return False

    service.delay_sampler = sample_delay
    service.delay_waiter = record_wait
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", ContinuingAdapter)

    result = await service.run_batch(BatchMode.MANUAL, progress=lambda *item: progress.append(item))

    assert [item.target_id for item in result.results] == [first_enabled.id, second_enabled.id]
    assert sampled_ranges == [(4, 9)]
    assert waited_seconds == [6.5]
    assert (target_label(second_enabled), "等待 6.5 秒后发送") in progress


@pytest.mark.asyncio
async def test_batch_cancellation_during_delay_cancels_unsent_targets(
    tmp_path, monkeypatch
) -> None:
    service, database = setup_service(tmp_path)
    disabled, first_enabled = database.list_targets()
    database.set_target_enabled(disabled.id, False)
    second_enabled = database.add_target(
        FriendCandidate("friend-3", "另一位好友", douyin_id="id-3"),
        "另一位好友",
    )
    cancel = threading.Event()

    async def cancel_wait(_seconds: float, event: threading.Event | None) -> bool:
        assert event is cancel
        event.set()
        return True

    service.delay_sampler = lambda _minimum, _maximum: 5.0
    service.delay_waiter = cancel_wait
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", ContinuingAdapter)

    result = await service.run_batch(BatchMode.MANUAL, cancel=cancel)

    assert [item.target_id for item in result.results] == [first_enabled.id, second_enabled.id]
    assert [item.status for item in result.results] == [
        AttemptStatus.SUCCESS,
        AttemptStatus.CANCELLED,
    ]
    assert database.has_daily_guard("account-key", first_enabled.id, today_iso())
    assert not database.has_daily_guard("account-key", second_enabled.id, today_iso())
    assert result.results[1].target_alias == target_label(second_enabled)


@pytest.mark.asyncio
async def test_validation_and_zero_delay_do_not_wait(tmp_path, monkeypatch) -> None:
    service, database = setup_service(tmp_path)
    disabled, _first_enabled = database.list_targets()
    database.set_target_enabled(disabled.id, False)
    database.add_target(
        FriendCandidate("friend-3", "另一位好友", douyin_id="id-3"),
        "另一位好友",
    )
    database.save_plan(
        enabled=False,
        send_time="09:00",
        message_text="测试消息",
        confirmed=True,
        delay_min_seconds=0,
        delay_max_seconds=0,
    )
    waited_seconds: list[float] = []

    async def record_wait(seconds: float, _cancel: threading.Event | None) -> bool:
        waited_seconds.append(seconds)
        return False

    service.delay_waiter = record_wait
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", ContinuingAdapter)

    await service.run_batch(BatchMode.MANUAL)
    validation = await service.run_batch(BatchMode.VALIDATION)

    assert waited_seconds == []
    assert [item.target_alias for item in validation.results] == [
        target_label(target) for target in database.list_targets(enabled_only=True)
    ]
    validation_events = [
        event for event in database.list_events() if event["category"] == "validation_success"
    ]
    assert {event["target_alias"] for event in validation_events} == {
        item.target_alias for item in validation.results
    }


@pytest.mark.asyncio
async def test_authentication_failure_stops_remaining_targets(tmp_path, monkeypatch) -> None:
    service, database = setup_service(tmp_path)
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", FatalAdapter)
    result = await service.run_batch(BatchMode.MANUAL)
    assert result.status is BatchStatus.FAILED
    assert [item.status for item in result.results] == [
        AttemptStatus.FAILED,
        AttemptStatus.CANCELLED,
    ]
    assert [item.target_alias for item in result.results] == [
        target_label(target) for target in database.list_targets()
    ]


@pytest.mark.asyncio
async def test_scheduled_batch_cannot_override_duplicate(tmp_path, monkeypatch) -> None:
    service, database = setup_service(tmp_path)
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", ContinuingAdapter)
    target = database.list_targets()[1]
    first = await service.run_batch(BatchMode.MANUAL, target_ids=[target.id])
    assert first.results[0].status is AttemptStatus.SUCCESS
    second = await service.run_batch(BatchMode.SCHEDULED, target_ids=[target.id])
    assert second.results[0].status is AttemptStatus.DUPLICATE
    assert database.has_daily_guard("account-key", target.id, today_iso())
    assert second.results[0].target_alias == target_label(target)


@pytest.mark.asyncio
async def test_visible_capture_returns_candidate_without_send_path(tmp_path, monkeypatch) -> None:
    service, database = setup_service(tmp_path)
    existing_target_keys = {target.stable_key for target in database.list_targets()}
    capture_requested = threading.Event()
    capture_requested.set()
    browser_ready = threading.Event()

    async def same_account(_session) -> Account:
        return Account("account-key", "测试账号", "now")

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", CaptureAdapter)
    monkeypatch.setattr("spark_keeper.automation.service.derive_account_identity", same_account)

    candidate = await service.capture_current_chat_friend(
        "测试好友乙",
        capture_requested=capture_requested,
        browser_ready=browser_ready,
    )

    assert browser_ready.is_set()
    assert candidate.display_name == "测试好友乙"
    assert {target.stable_key for target in database.list_targets()} == existing_target_keys
    assert candidate.stable_key not in existing_target_keys
    assert any(event["category"] == "friend_captured" for event in database.list_events())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["lookup", "before_trigger", "after_trigger", "domain"])
async def test_target_errors_keep_full_details_and_existing_send_safety(
    tmp_path, monkeypatch, failure_phase
) -> None:
    service, database = setup_service(tmp_path)
    target = database.list_targets()[1]
    progress: list[tuple[str, str]] = []
    original = "浏览器查找原文 storage_state\n" + "详情" * 200

    def fail() -> None:
        try:
            raise ValueError(original)
        except ValueError as cause:
            error = TargetNotFound() if failure_phase == "domain" else RuntimeError("页面操作失败")
            error.add_note("lookup: query=成功好友; candidates=0")
            raise error from cause

    class FailingAdapter(ContinuingAdapter):
        async def open_confirmed_target(self, _target) -> None:
            if failure_phase in {"lookup", "domain"}:
                fail()

        async def send_text_and_confirm(self, _text, on_trigger) -> None:
            if failure_phase == "after_trigger":
                on_trigger()
            fail()

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", FailingAdapter)
    result = await service.run_batch(
        BatchMode.MANUAL,
        target_ids=[target.id],
        progress=lambda *item: progress.append(item),
    )

    item = result.results[0]
    expected_status = (
        AttemptStatus.UNKNOWN if failure_phase == "after_trigger" else AttemptStatus.FAILED
    )
    expected_code = (
        ErrorCode.TARGET_NOT_FOUND
        if failure_phase == "domain"
        else ErrorCode.SEND_UNKNOWN
        if failure_phase == "after_trigger"
        else ErrorCode.INTERNAL_ERROR
    )
    assert item.status is expected_status
    assert item.error_code is expected_code
    assert item.target_alias == target_label(target)
    assert f"ValueError: {original}" in item.detail
    assert "lookup: query=成功好友; candidates=0" in item.detail
    assert "direct cause" in item.detail
    assert "Traceback (most recent call last)" in item.detail
    assert "in fail" in item.detail
    event = next(event for event in database.list_events() if event["category"] == expected_code)
    assert event["message"] == item.detail
    assert event["target_alias"] == item.target_alias
    assert (item.target_alias, item.detail) in progress
    assert database.has_daily_guard("account-key", target.id, today_iso()) == (
        failure_phase == "after_trigger"
    )
    attempt = database.get_attempt(item.attempt_id)
    assert attempt["status"] == expected_status
    assert bool(attempt["triggered_at"]) == (failure_phase == "after_trigger")


@pytest.mark.asyncio
@pytest.mark.parametrize("domain_error", [False, True])
async def test_batch_boundary_preserves_original_error_chain(tmp_path, monkeypatch, domain_error):
    service, database = setup_service(tmp_path)

    class FailingOpenAdapter(ContinuingAdapter):
        async def open_chat(self) -> None:
            try:
                raise ValueError("browser open failed: 原始原因")
            except ValueError as cause:
                error = AuthenticationRequired() if domain_error else RuntimeError("浏览器启动失败")
                error.add_note("phase=open_chat")
                raise error from cause

    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", FailingOpenAdapter)
    result = await service.run_batch(BatchMode.MANUAL)
    code = ErrorCode.AUTHENTICATION_REQUIRED if domain_error else ErrorCode.INTERNAL_ERROR

    assert result.status is BatchStatus.FAILED
    assert result.error_code is code
    assert result.results == ()
    event = next(event for event in database.list_events() if event["category"] == code)
    assert "ValueError: browser open failed: 原始原因" in event["message"]
    assert "phase=open_chat" in event["message"]
    assert "Traceback (most recent call last)" in event["message"]
    assert "in open_chat" in event["message"]


def test_saved_friend_event_uses_real_target_label(tmp_path) -> None:
    service, database = setup_service(tmp_path)
    target = service.save_friend(FriendCandidate("friend-new", "新增好友"), "新增好友")

    event = next(event for event in database.list_events() if event["category"] == "friend_saved")
    assert event["target_alias"] == target_label(target)


@pytest.mark.asyncio
async def test_pre_cancelled_batch_keeps_real_labels_for_every_target(tmp_path, monkeypatch):
    service, database = setup_service(tmp_path)
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr("spark_keeper.automation.service.DouyinChatAdapter", ContinuingAdapter)

    result = await service.run_batch(BatchMode.MANUAL, cancel=cancel)

    assert result.status is BatchStatus.CANCELLED
    assert [item.target_alias for item in result.results] == [
        target_label(target) for target in database.list_targets()
    ]
    assert all(item.status is AttemptStatus.CANCELLED for item in result.results)
