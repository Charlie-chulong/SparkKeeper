from __future__ import annotations

import pytest

from spark_keeper.automation.send_state import (
    DeliveryOutcome,
    DeliverySample,
    await_delivery_terminal,
)


class Timeline:
    def __init__(self, frames: list[DeliverySample]) -> None:
        self.frames = frames
        self.index = 0
        self.now = 0.0

    async def sample(self) -> DeliverySample:
        return self.frames[min(self.index, len(self.frames) - 1)]

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.index += 1

    def clock(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_clean_new_message_must_remain_stable() -> None:
    timeline = Timeline([DeliverySample(True, False, False)])
    outcome = await await_delivery_terminal(
        timeline.sample,
        timeout_seconds=3,
        initial_clean_seconds=1,
        poll_seconds=0.25,
        clock=timeline.clock,
        sleep=timeline.sleep,
    )
    assert outcome is DeliveryOutcome.SUCCESS
    assert timeline.now >= 1


@pytest.mark.asyncio
async def test_pending_then_clean_reaches_success() -> None:
    timeline = Timeline(
        [
            DeliverySample(True, True, False),
            DeliverySample(True, True, False),
            DeliverySample(True, False, False),
        ]
    )
    outcome = await await_delivery_terminal(
        timeline.sample,
        timeout_seconds=3,
        stable_seconds=0.5,
        poll_seconds=0.25,
        clock=timeline.clock,
        sleep=timeline.sleep,
    )
    assert outcome is DeliveryOutcome.SUCCESS


@pytest.mark.asyncio
async def test_late_failure_wins_over_visible_bubble() -> None:
    timeline = Timeline(
        [
            DeliverySample(True, False, False),
            DeliverySample(True, False, True),
        ]
    )
    outcome = await await_delivery_terminal(
        timeline.sample,
        timeout_seconds=3,
        initial_clean_seconds=1,
        poll_seconds=0.25,
        clock=timeline.clock,
        sleep=timeline.sleep,
    )
    assert outcome is DeliveryOutcome.FAILED


@pytest.mark.asyncio
async def test_missing_new_message_is_unknown() -> None:
    timeline = Timeline([DeliverySample(False, False, False)])
    outcome = await await_delivery_terminal(
        timeline.sample,
        timeout_seconds=1,
        poll_seconds=0.25,
        clock=timeline.clock,
        sleep=timeline.sleep,
    )
    assert outcome is DeliveryOutcome.UNKNOWN
