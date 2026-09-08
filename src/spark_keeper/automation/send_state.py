from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class DeliveryOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeliverySample:
    new_matching_outgoing: bool
    pending: bool
    failed: bool


SampleProvider = Callable[[], Awaitable[DeliverySample]]
SleepProvider = Callable[[float], Awaitable[None]]
ClockProvider = Callable[[], float]


async def await_delivery_terminal(
    sample: SampleProvider,
    *,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.25,
    initial_clean_seconds: float = 1.5,
    stable_seconds: float = 0.75,
    clock: ClockProvider | None = None,
    sleep: SleepProvider | None = None,
) -> DeliveryOutcome:
    """观察新己方消息，只有稳定终态才返回成功。

    新气泡刚出现时可能尚未挂载发送中或失败标记，因此需要持续观察。
    超时永远返回 UNKNOWN，不把缺少失败标记当作成功。
    """

    loop = asyncio.get_running_loop()
    now = clock or loop.time
    pause = sleep or asyncio.sleep
    deadline = now() + timeout_seconds
    matched_at: float | None = None
    clean_since: float | None = None
    saw_pending = False

    while now() < deadline:
        state = await sample()
        if state.failed:
            return DeliveryOutcome.FAILED
        if not state.new_matching_outgoing:
            matched_at = None
            clean_since = None
            await pause(poll_seconds)
            continue

        current = now()
        if matched_at is None:
            matched_at = current
        if state.pending:
            saw_pending = True
            clean_since = None
            await pause(poll_seconds)
            continue

        if clean_since is None:
            clean_since = current
        required_clean = stable_seconds if saw_pending else initial_clean_seconds
        if current - clean_since >= required_clean:
            return DeliveryOutcome.SUCCESS
        await pause(poll_seconds)

    return DeliveryOutcome.UNKNOWN
