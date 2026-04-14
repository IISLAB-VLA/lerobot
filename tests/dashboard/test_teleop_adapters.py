"""Tests for the teleop adapter scaffolding (task #12 prep).

Concrete hardware-backed sources (LeaderArmEventSource) land after
task #6; these tests cover the synthetic ``CallableLeaderSource`` and
the protocol contract so the dispatcher (task #11 follow-up) can be
built against a stable base.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

import pytest

from lerobot.dashboard.teleop.adapters import (
    CallableLeaderSource,
    SourceState,
    TeleopEventSource,
)

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


def test_callable_leader_source_satisfies_protocol():
    src = CallableLeaderSource(lambda: {"j1": 0.0}, rate_hz=10.0)
    assert isinstance(src, TeleopEventSource)


def test_callable_leader_source_emits_polled_values():
    values = iter([{"j1": 0.1}, {"j1": 0.2}, {"j1": 0.3}])
    src = CallableLeaderSource(lambda: next(values), rate_hz=200.0)

    async def scenario() -> list[dict[str, float]]:
        await src.start()
        collected: list[dict[str, float]] = []
        async for payload in src.events():
            collected.append(payload)
            if len(collected) == 3:
                break
        await src.stop()
        return collected

    got = _run(scenario())
    assert [p["j1"] for p in got] == [0.1, 0.2, 0.3]
    assert src.state is SourceState.STOPPED


def test_callable_leader_source_reports_failure_on_bad_return():
    bad: Callable[[], object] = lambda: "not a dict"
    src = CallableLeaderSource(bad, rate_hz=50.0)  # type: ignore[arg-type]

    async def scenario() -> SourceState:
        await src.start()
        # Give the internal loop a moment to crash.
        await asyncio.sleep(0.1)
        await src.stop()
        return src.state

    # We expect the background task to have raised; stop() cancels it.
    _run(scenario())
    # FAILED is latched before stop() resets it — verify the loop really
    # crashed by starting again and checking the same bad return still trips
    # the state transition from STOPPED -> STARTING -> RUNNING -> FAILED.
    src2 = CallableLeaderSource(bad, rate_hz=50.0)  # type: ignore[arg-type]

    async def scenario2() -> SourceState:
        await src2.start()
        await asyncio.sleep(0.1)
        state = src2.state
        await src2.stop()
        return state

    assert _run(scenario2()) is SourceState.FAILED


def test_callable_leader_source_rejects_subhertz_rate():
    with pytest.raises(ValueError):
        CallableLeaderSource(lambda: {"j1": 0.0}, rate_hz=0.5)


def test_callable_leader_source_accepts_async_callable():
    async def get_action() -> dict[str, float]:
        return {"j1": 0.5}

    src = CallableLeaderSource(get_action, rate_hz=100.0)

    async def scenario() -> dict[str, float]:
        await src.start()
        async for payload in src.events():
            await src.stop()
            return payload
        raise AssertionError("no event emitted")

    assert _run(scenario()) == {"j1": 0.5}
