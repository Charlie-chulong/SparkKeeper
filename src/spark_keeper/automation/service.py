from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime

from ..database import Database, today_iso
from ..dpapi import DpapiJsonStore
from ..logging_safe import format_error, target_label
from ..models import (
    AttemptStatus,
    BatchMode,
    BatchResult,
    BatchStatus,
    ErrorCode,
    FriendCandidate,
    MessageKind,
    SparkScanResult,
    Target,
    TargetResult,
)
from ..mutex import AlreadyRunningError, WindowsTaskMutex
from .browser import BrowserSessionFactory
from .douyin_chat import DouyinChatAdapter, derive_account_identity, interactive_login
from .errors import AutomationError, SendFailed, SendUnknown
from .spark_scan import scan_contacts

ProgressCallback = Callable[[str, str], None]
StatusCallback = Callable[[str], None]
DelaySampler = Callable[[float, float], float]
DelayWaiter = Callable[[float, threading.Event | None], Awaitable[bool]]


async def wait_for_delay(seconds: float, cancel: threading.Event | None) -> bool:
    if seconds <= 0:
        return bool(cancel and cancel.is_set())
    if cancel is None:
        await asyncio.sleep(seconds)
        return False
    return await asyncio.to_thread(cancel.wait, seconds)


class BatchService:
    def __init__(
        self,
        database: Database,
        state_store: DpapiJsonStore,
        *,
        delay_sampler: DelaySampler = random.uniform,
        delay_waiter: DelayWaiter = wait_for_delay,
    ) -> None:
        self.database = database
        self.state_store = state_store
        self.browser_factory = BrowserSessionFactory(state_store)
        self.delay_sampler = delay_sampler
        self.delay_waiter = delay_waiter

    async def login(self, status: StatusCallback | None = None) -> None:
        with WindowsTaskMutex():
            result = await interactive_login(self.browser_factory, status=status)
            self.database.save_account(
                result.account.platform_user_id,
                result.account.display_name,
            )
            self.database.record_event("INFO", "login", "扫码登录状态已安全保存")

    def logout(self) -> None:
        self.state_store.delete()
        self.database.clear_account()
        self.database.record_event("INFO", "logout", "本机登录状态已清除")

    async def search_friends(self, query: str) -> list[FriendCandidate]:
        if self.database.get_account() is None or not self.state_store.exists():
            raise ValueError("请先扫码登录")
        with WindowsTaskMutex():
            async with self.browser_factory.open(headless=True, use_saved_state=True) as session:
                adapter = DouyinChatAdapter(session.page)
                await adapter.open_chat()
                candidates = await adapter.search_targets(query)
                self.database.record_event(
                    "INFO",
                    "friend_search",
                    f"好友搜索完成，候选数量 {len(candidates)}",
                )
                return candidates

    async def capture_current_chat_friend(
        self,
        expected_name: str,
        *,
        capture_requested: threading.Event,
        browser_ready: threading.Event,
        cancel: threading.Event | None = None,
        status: StatusCallback | None = None,
    ) -> FriendCandidate:
        """让用户在应用账号的可见浏览器中选定聊天，然后只读提取身份。"""

        account = self.database.get_account()
        if account is None or not self.state_store.exists():
            raise ValueError("请先扫码登录")
        expected_name = " ".join(expected_name.split()).strip()
        if not expected_name:
            raise ValueError("请填写右侧聊天顶部显示的准确名称")
        with WindowsTaskMutex():
            async with self.browser_factory.open(headless=False, use_saved_state=True) as session:
                adapter = DouyinChatAdapter(session.page)
                await adapter.open_chat()
                visible_account = await derive_account_identity(session)
                if visible_account.platform_user_id != account.platform_user_id:
                    raise AutomationError(
                        ErrorCode.CONFIGURATION_INVALID,
                        "可见浏览器账号与应用保存账号不一致，请重新扫码登录",
                        fatal=True,
                    )
                browser_ready.set()
                if status:
                    status("请在可见浏览器中按备注名或昵称打开目标好友聊天，然后返回软件读取")
                while not capture_requested.is_set():
                    if cancel is not None and cancel.is_set():
                        raise AutomationError(ErrorCode.CANCELLED, "好友选择已取消")
                    if session.page.is_closed():
                        raise AutomationError(ErrorCode.CANCELLED, "好友选择浏览器已关闭")
                    await session.page.wait_for_timeout(200)
                candidate = await adapter.capture_current_chat_candidate(expected_name)
                self.database.record_event(
                    "INFO",
                    "friend_captured",
                    "已从用户手动打开的当前聊天提取一位候选，尚未保存",
                )
                return candidate

    def save_friend(self, candidate: FriendCandidate, search_query: str) -> Target:
        target = self.database.add_target(candidate, search_query)
        self.database.record_event(
            "INFO",
            "friend_saved",
            "已确认并保存一位好友",
            target_alias=target_label(target),
        )
        return target

    async def scan_spark_contacts(
        self,
        *,
        progress: StatusCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> SparkScanResult:
        """只读扫描并绑定当前账号；不搜索、打开收件人或触碰发送路径。"""
        with WindowsTaskMutex():
            account = self.database.get_account()
            if account is None or not self.state_store.exists():
                raise ValueError("请先扫码登录")
            async with self.browser_factory.open(headless=True, use_saved_state=True) as session:
                adapter = DouyinChatAdapter(session.page)
                await adapter.open_chat()
                visible_account = await derive_account_identity(session)
                if visible_account.platform_user_id != account.platform_user_id:
                    raise AutomationError(
                        ErrorCode.CONFIGURATION_INVALID,
                        "扫描浏览器账号与应用保存账号不一致，请重新扫码登录",
                        fatal=True,
                    )
                result = await scan_contacts(adapter, account, progress=progress, cancel=cancel)
                # Logout does not acquire the automation mutex, so compare both the
                # persisted login generation and browser identity after scanning.
                current_account = self.database.get_account()
                try:
                    await adapter._require_ready()
                except AutomationError as error:
                    if error.code is not ErrorCode.PAGE_NOT_READY:
                        raise
                visible_account = await derive_account_identity(session)
                if (
                    current_account is None
                    or current_account.platform_user_id != account.platform_user_id
                    or current_account.logged_in_at != account.logged_in_at
                    or visible_account.platform_user_id != account.platform_user_id
                ):
                    raise AutomationError(
                        ErrorCode.CONFIGURATION_INVALID,
                        "扫描期间账号或登录状态已变化，请重新扫描",
                        fatal=True,
                    )
                self.database.record_event(
                    "INFO",
                    "spark_scan",
                    f"火花只读扫描结束，状态 {result.status.value}，候选数量 {result.scanned_count}",
                )
                return result

    def import_spark_contacts(
        self,
        scan: SparkScanResult,
        selected_keys: Iterable[str],
    ) -> tuple[int, int]:
        with WindowsTaskMutex():
            return self.database.import_spark_contacts(scan, selected_keys)

    async def validate_config(
        self,
        *,
        message_text_override: str | None = None,
        message_kind_override: MessageKind | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> BatchResult:
        return await self.run_batch(
            BatchMode.VALIDATION,
            message_text_override=message_text_override,
            message_kind_override=message_kind_override,
            progress=progress,
            cancel=cancel,
        )

    async def run_batch(
        self,
        mode: BatchMode,
        *,
        manual_override_target_ids: Iterable[int] = (),
        target_ids: Iterable[int] | None = None,
        scheduled_for: str | None = None,
        message_text_override: str | None = None,
        message_kind_override: MessageKind | None = None,
        progress: ProgressCallback | None = None,
        inter_target_delay_override: tuple[int, int] | None = None,
        cancel: threading.Event | None = None,
    ) -> BatchResult:
        account = self.database.get_account()
        if account is None or not self.state_store.exists():
            raise ValueError("请先扫码登录")
        plan = self.database.get_plan()
        if (message_text_override is None) != (message_kind_override is None):
            raise ValueError("补跑消息快照必须同时提供消息类型与文本")
        message_kind = MessageKind(
            plan.message_kind if message_kind_override is None else message_kind_override
        )
        message_text = plan.message_text if message_text_override is None else message_text_override
        if message_kind is MessageKind.SPARK_STICKER:
            message_text = ""
        targets = self._select_targets(target_ids)
        if not targets:
            raise ValueError("至少需要启用一位好友")
        if inter_target_delay_override is None:
            delay_min_seconds = plan.delay_min_seconds
            delay_max_seconds = plan.delay_max_seconds
        else:
            delay_min_seconds, delay_max_seconds = inter_target_delay_override
        self.database.validate_delay_range(delay_min_seconds, delay_max_seconds)
        if (
            mode is not BatchMode.VALIDATION
            and message_kind is MessageKind.TEXT
            and not message_text.strip()
        ):
            raise ValueError("请先填写固定消息文本")
        overrides = {int(value) for value in manual_override_target_ids}
        if mode is BatchMode.SCHEDULED and overrides:
            raise ValueError("定时任务不能覆盖当天防重复")
        if not overrides.issubset({target.id for target in targets}):
            raise ValueError("人工覆盖目标不在本批次中")

        try:
            mutex = WindowsTaskMutex()
            mutex.acquire()
        except AlreadyRunningError as exc:
            batch_id = self.database.start_batch(
                mode,
                target_count=len(targets),
                scheduled_for=scheduled_for,
            )
            self.database.record_event(
                "WARNING",
                ErrorCode.ALREADY_RUNNING.value,
                format_error(exc),
                batch_id=batch_id,
            )
            self.database.finish_batch(
                batch_id,
                BatchStatus.SKIPPED,
                error_code=ErrorCode.ALREADY_RUNNING,
            )
            return BatchResult(
                batch_id,
                mode,
                BatchStatus.SKIPPED,
                (),
                ErrorCode.ALREADY_RUNNING,
            )

        try:
            batch_id = self.database.start_batch(
                mode,
                target_count=len(targets),
                scheduled_for=scheduled_for,
            )
            results: list[TargetResult] = []
            fatal_error: ErrorCode | None = None
            self.database.record_event(
                "INFO",
                "batch_started",
                f"批次开始，模式 {mode.value}，目标数量 {len(targets)}",
                batch_id=batch_id,
            )
        except BaseException:
            mutex.release()
            raise
        try:
            async with self.browser_factory.open(headless=True, use_saved_state=True) as session:
                adapter = DouyinChatAdapter(session.page)
                await adapter.open_chat()
                previous_send_attempt = False
                for index, target in enumerate(targets):
                    label = target_label(target)
                    run_date = today_iso()
                    will_attempt_send = mode is not BatchMode.VALIDATION and (
                        target.id in overrides
                        or not self.database.has_daily_guard(
                            account.platform_user_id,
                            target.id,
                            run_date,
                        )
                    )
                    if cancel is not None and cancel.is_set():
                        results.extend(self._cancel_remaining(targets[index:]))
                        break
                    if previous_send_attempt and will_attempt_send:
                        delay_seconds = self.delay_sampler(
                            delay_min_seconds,
                            delay_max_seconds,
                        )
                        cancelled_during_wait = False
                        if delay_seconds > 0:
                            if progress:
                                progress(label, f"等待 {delay_seconds:.1f} 秒后发送")
                            cancelled_during_wait = await self.delay_waiter(delay_seconds, cancel)
                        if cancelled_during_wait or (cancel is not None and cancel.is_set()):
                            results.extend(self._cancel_remaining(targets[index:]))
                            break
                    if progress:
                        progress(label, "正在确认好友")
                    attempt_id: str | None = None
                    try:
                        await adapter.open_confirmed_target(target)
                        if mode is BatchMode.VALIDATION:
                            if message_kind is MessageKind.SPARK_STICKER:
                                await adapter.validate_spark_sticker(target)
                            elif not await adapter.composer_available():
                                raise AutomationError(
                                    ErrorCode.COMPOSER_UNAVAILABLE,
                                    "聊天输入框不可用",
                                )
                            result = TargetResult(target.id, label, AttemptStatus.SUCCESS)
                            results.append(result)
                            self.database.record_event(
                                "INFO",
                                "validation_success",
                                "好友配置验证成功",
                                batch_id=batch_id,
                                target_alias=label,
                            )
                            if progress:
                                progress(label, "验证成功")
                            continue

                        reservation = self.database.reserve_attempt(
                            batch_id=batch_id,
                            account_key=account.platform_user_id,
                            target_id=target.id,
                            run_date=run_date,
                            message_text=message_text,
                            message_kind=message_kind,
                            manual_override=target.id in overrides,
                        )
                        attempt_id = reservation.attempt_id
                        if not reservation.allowed:
                            result = TargetResult(
                                target.id,
                                label,
                                AttemptStatus.DUPLICATE,
                                ErrorCode.DUPLICATE_BLOCKED,
                                "当天已有成功或结果不确定的发送记录",
                                attempt_id,
                            )
                            results.append(result)
                            self.database.record_event(
                                "INFO",
                                ErrorCode.DUPLICATE_BLOCKED.value,
                                "当天防重复已阻止发送",
                                batch_id=batch_id,
                                target_alias=label,
                            )
                            if progress:
                                progress(label, "当天重复，已跳过")
                            continue
                        previous_send_attempt = True

                        if progress:
                            progress(label, "正在发送")

                        def on_trigger(attempt_id: str = attempt_id) -> None:
                            self.database.mark_attempt_triggered(attempt_id)

                        if message_kind is MessageKind.SPARK_STICKER:
                            await adapter.send_spark_sticker_and_confirm(target, on_trigger)
                        else:
                            await adapter.verify_recipient(target)
                            await adapter.send_text_and_confirm(message_text, on_trigger)
                        self.database.finish_attempt(attempt_id, AttemptStatus.SUCCESS)
                        results.append(
                            TargetResult(
                                target.id,
                                label,
                                AttemptStatus.SUCCESS,
                                attempt_id=attempt_id,
                            )
                        )
                        self.database.record_event(
                            "INFO",
                            "send_success",
                            "消息已确认发送成功",
                            batch_id=batch_id,
                            target_alias=label,
                        )
                        if progress:
                            progress(label, "发送成功")
                    except AutomationError as exc:
                        detail = format_error(exc)
                        if attempt_id is None:
                            result_status = AttemptStatus.FAILED
                            if mode is not BatchMode.VALIDATION:
                                attempt_id = self.database.record_terminal_attempt(
                                    batch_id=batch_id,
                                    account_key=account.platform_user_id,
                                    target_id=target.id,
                                    run_date=run_date,
                                    message_text=message_text,
                                    message_kind=message_kind,
                                    status=AttemptStatus.FAILED,
                                    error_code=exc.code,
                                )
                        else:
                            result_status = self._persist_automation_failure(attempt_id, exc)
                        result_code = (
                            ErrorCode.SEND_UNKNOWN
                            if result_status is AttemptStatus.UNKNOWN
                            else exc.code
                        )
                        results.append(
                            TargetResult(
                                target.id,
                                label,
                                result_status,
                                result_code,
                                detail,
                                attempt_id,
                            )
                        )
                        self.database.record_event(
                            "ERROR" if exc.fatal else "WARNING",
                            result_code.value,
                            detail,
                            batch_id=batch_id,
                            target_alias=label,
                        )
                        self._create_attention_action(exc, batch_id)
                        if result_status is AttemptStatus.UNKNOWN and attempt_id:
                            self.database.create_pending_action(
                                "unknown_send",
                                f"unknown:{attempt_id}",
                                {"attempt_id": attempt_id, "batch_id": batch_id},
                            )
                        if progress:
                            progress(label, detail)
                        if exc.fatal:
                            fatal_error = exc.code
                            results.extend(self._cancel_remaining(targets[index + 1 :]))
                            break
                    except Exception as exc:  # noqa: BLE001
                        detail = format_error(exc)
                        if attempt_id is None:
                            status, code = AttemptStatus.FAILED, ErrorCode.INTERNAL_ERROR
                            if mode is not BatchMode.VALIDATION:
                                attempt_id = self.database.record_terminal_attempt(
                                    batch_id=batch_id,
                                    account_key=account.platform_user_id,
                                    target_id=target.id,
                                    run_date=run_date,
                                    message_text=message_text,
                                    message_kind=message_kind,
                                    status=status,
                                    error_code=code,
                                )
                        else:
                            status, code = self._persist_unexpected_failure(attempt_id)
                        results.append(
                            TargetResult(
                                target.id,
                                label,
                                status,
                                code,
                                detail,
                                attempt_id,
                            )
                        )
                        self.database.record_event(
                            "ERROR",
                            code.value,
                            detail,
                            batch_id=batch_id,
                            target_alias=label,
                        )
                        if status is AttemptStatus.UNKNOWN and attempt_id:
                            self.database.create_pending_action(
                                "unknown_send",
                                f"unknown:{attempt_id}",
                                {"attempt_id": attempt_id, "batch_id": batch_id},
                            )
                        if progress:
                            progress(label, detail)
        except AutomationError as exc:
            fatal_error = exc.code
            self.database.record_event(
                "ERROR",
                exc.code.value,
                format_error(exc),
                batch_id=batch_id,
            )
            self._create_attention_action(exc, batch_id)
        except Exception as exc:  # noqa: BLE001
            fatal_error = ErrorCode.INTERNAL_ERROR
            self.database.record_event(
                "ERROR",
                ErrorCode.INTERNAL_ERROR.value,
                format_error(exc),
                batch_id=batch_id,
            )
        finally:
            mutex.release()

        status = self._batch_status(results, fatal_error)
        self.database.finish_batch(batch_id, status, error_code=fatal_error)
        self.database.record_event(
            "INFO",
            "batch_finished",
            self._batch_summary(status, results),
            batch_id=batch_id,
        )
        return BatchResult(batch_id, mode, status, tuple(results), fatal_error)

    def _select_targets(self, target_ids: Iterable[int] | None) -> list[Target]:
        enabled = self.database.list_targets(enabled_only=True)
        if target_ids is None:
            return enabled
        selected = {int(value) for value in target_ids}
        return [target for target in enabled if target.id in selected]

    def _persist_automation_failure(
        self,
        attempt_id: str | None,
        error: AutomationError,
    ) -> AttemptStatus:
        if attempt_id is None:
            return AttemptStatus.FAILED
        if isinstance(error, SendFailed):
            self.database.finish_attempt(
                attempt_id,
                AttemptStatus.FAILED,
                error_code=error.code,
            )
            return AttemptStatus.FAILED
        attempt = self.database.get_attempt(attempt_id)
        if (
            isinstance(error, SendUnknown)
            or error.send_triggered
            or (attempt and attempt.get("triggered_at"))
        ):
            self.database.finish_attempt(
                attempt_id,
                AttemptStatus.UNKNOWN,
                error_code=ErrorCode.SEND_UNKNOWN,
            )
            return AttemptStatus.UNKNOWN
        self.database.finish_attempt(
            attempt_id,
            AttemptStatus.FAILED,
            error_code=error.code,
        )
        return AttemptStatus.FAILED

    def _persist_unexpected_failure(
        self,
        attempt_id: str | None,
    ) -> tuple[AttemptStatus, ErrorCode]:
        if attempt_id is None:
            return AttemptStatus.FAILED, ErrorCode.INTERNAL_ERROR
        attempt = self.database.get_attempt(attempt_id)
        if attempt and attempt.get("triggered_at"):
            self.database.finish_attempt(
                attempt_id,
                AttemptStatus.UNKNOWN,
                error_code=ErrorCode.SEND_UNKNOWN,
            )
            return AttemptStatus.UNKNOWN, ErrorCode.SEND_UNKNOWN
        self.database.finish_attempt(
            attempt_id,
            AttemptStatus.FAILED,
            error_code=ErrorCode.INTERNAL_ERROR,
        )
        return AttemptStatus.FAILED, ErrorCode.INTERNAL_ERROR

    def _create_attention_action(
        self,
        error: AutomationError,
        batch_id: str,
    ) -> None:
        if error.code in {
            ErrorCode.AUTHENTICATION_REQUIRED,
            ErrorCode.HUMAN_VERIFICATION_REQUIRED,
            ErrorCode.LOGIN_STATE_UNAVAILABLE,
        }:
            day = datetime.now().astimezone().date().isoformat()
            self.database.create_pending_action(
                "login_required",
                f"login:{day}",
                {"error_code": error.code.value, "batch_id": batch_id},
            )

    @staticmethod
    def _cancel_remaining(targets: list[Target]) -> list[TargetResult]:
        return [
            TargetResult(
                target.id,
                target_label(target),
                AttemptStatus.CANCELLED,
                ErrorCode.CANCELLED,
                "批次已停止，尚未触发发送",
            )
            for target in targets
        ]

    @staticmethod
    def _batch_status(
        results: list[TargetResult],
        fatal_error: ErrorCode | None,
    ) -> BatchStatus:
        if fatal_error and not any(result.status is AttemptStatus.SUCCESS for result in results):
            return BatchStatus.FAILED
        if not results:
            return BatchStatus.FAILED if fatal_error else BatchStatus.SUCCESS
        if all(
            result.status in {AttemptStatus.SUCCESS, AttemptStatus.DUPLICATE} for result in results
        ):
            return BatchStatus.SUCCESS
        if any(result.status is AttemptStatus.SUCCESS for result in results):
            return BatchStatus.PARTIAL
        if all(result.status is AttemptStatus.CANCELLED for result in results):
            return BatchStatus.CANCELLED
        return BatchStatus.FAILED

    @staticmethod
    def _batch_summary(status: BatchStatus, results: list[TargetResult]) -> str:
        success = sum(result.status is AttemptStatus.SUCCESS for result in results)
        failed = sum(result.status is AttemptStatus.FAILED for result in results)
        unknown = sum(result.status is AttemptStatus.UNKNOWN for result in results)
        duplicate = sum(result.status is AttemptStatus.DUPLICATE for result in results)
        return (
            f"批次结束，状态 {status.value}，成功 {success}，失败 {failed}，"
            f"结果不确定 {unknown}，重复跳过 {duplicate}"
        )
