"""Concrete adapter tests for :class:`LeaderArmEventSource` and
:class:`GamepadEventSource` (task #12).

Hardware-level Teleoperator and the browser's Gamepad API are both
stubbed out so these tests run on any machine. The goal is to verify
the lifecycle, event flow, and backpressure semantics the dispatcher
relies on.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

import pytest

from lerobot.dashboard.teleop.adapters import (
    GamepadEventSource,
    LeaderArmEventSource,
    LinearAxisMapping,
    SourceState,
    TeleoperatorPoller,
)
from lerobot.dashboard.teleop.protocol import GamepadEvent

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# LeaderArmEventSource
# ---------------------------------------------------------------------------


class StubTeleoperator:
    """Duck-typed Teleoperator that satisfies :class:`TeleoperatorPoller`."""

    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False
        self._tick = 0
        self.connect_raises = False

    def connect(self) -> None:
        if self.connect_raises:
            raise RuntimeError("simulated connect failure")
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def get_action(self) -> dict[str, float]:
        self._tick += 1
        return {"j1": float(self._tick) * 0.1}


def test_leader_arm_source_satisfies_protocol():
    src = LeaderArmEventSource(StubTeleoperator(), rate_hz=50.0)
    assert isinstance(src._teleop, TeleoperatorPoller)  # noqa: SLF001


def test_leader_arm_source_streams_get_action_values():
    teleop = StubTeleoperator()
    src = LeaderArmEventSource(teleop, rate_hz=200.0)

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
    assert [p["j1"] for p in got] == [pytest.approx(0.1), pytest.approx(0.2), pytest.approx(0.3)]
    assert teleop.connected is True
    assert teleop.disconnected is True
    assert src.state is SourceState.STOPPED


def test_leader_arm_source_propagates_connect_failure():
    teleop = StubTeleoperator()
    teleop.connect_raises = True
    src = LeaderArmEventSource(teleop, rate_hz=50.0)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            await src.start()
        # Even on failed start, stop() must be safe to call (dispatcher
        # fires it from a finally: block).
        await src.stop()

    _run(scenario())
    assert teleop.disconnected is True
    assert src.state is SourceState.FAILED


# ---------------------------------------------------------------------------
# LinearAxisMapping
# ---------------------------------------------------------------------------


def test_linear_mapping_applies_deadzone_and_scale():
    mapping = LinearAxisMapping(
        axis_joints=("j1", "j2", None, "j3"),
        scale=2.0,
        deadzone=0.1,
    )
    # j1 within deadzone -> 0; j2 above deadzone -> rescaled; None -> skipped; j3 negative.
    event = GamepadEvent(axes=(0.05, 0.55, 0.9, -0.55), buttons=())
    action = mapping.translate(event)
    assert action is not None
    assert action["j1"] == 0.0
    assert action["j2"] == pytest.approx(1.0)  # (0.55 - 0.1) / (1 - 0.1) * 2.0
    assert "unused" not in action and len(action) == 3
    assert action["j3"] == pytest.approx(-1.0)


def test_linear_mapping_rejects_invalid_deadzone():
    with pytest.raises(ValueError):
        LinearAxisMapping(axis_joints=("j1",), deadzone=1.0)


def test_linear_mapping_returns_none_for_empty_axes():
    mapping = LinearAxisMapping(axis_joints=("j1",))
    assert mapping.translate(GamepadEvent(axes=(), buttons=())) is None


# ---------------------------------------------------------------------------
# GamepadEventSource
# ---------------------------------------------------------------------------


def test_gamepad_source_translates_fed_events():
    mapping = LinearAxisMapping(axis_joints=("j1", "j2"), deadzone=0.0)
    source = GamepadEventSource(mapping=mapping)

    async def scenario() -> list[dict[str, float]]:
        await source.start()
        await source.feed(GamepadEvent(axes=(0.2, -0.3), buttons=()))
        await source.feed(GamepadEvent(axes=(0.4, 0.1), buttons=()))
        collected: list[dict[str, float]] = []
        async for payload in source.events():
            collected.append(payload)
            if len(collected) == 2:
                break
        await source.stop()
        return collected

    got = _run(scenario())
    assert got == [
        {"j1": pytest.approx(0.2), "j2": pytest.approx(-0.3)},
        {"j1": pytest.approx(0.4), "j2": pytest.approx(0.1)},
    ]


def test_gamepad_source_drops_oldest_on_queue_overflow():
    mapping = LinearAxisMapping(axis_joints=("j1",), deadzone=0.0)
    source = GamepadEventSource(mapping=mapping, queue_size=2)

    async def scenario() -> list[dict[str, float]]:
        await source.start()
        # Saturate the queue with 5 distinct values; the consumer should
        # see the last two (0.4 and 0.5) because earlier frames were
        # evicted by the drop-oldest policy.
        for v in (0.1, 0.2, 0.3, 0.4, 0.5):
            await source.feed(GamepadEvent(axes=(v,), buttons=()))
        collected: list[dict[str, float]] = []
        async for payload in source.events():
            collected.append(payload)
            if len(collected) == 2:
                break
        await source.stop()
        return collected

    got = _run(scenario())
    assert [round(p["j1"], 2) for p in got] == [0.4, 0.5]


def test_gamepad_source_feed_is_noop_when_stopped():
    mapping = LinearAxisMapping(axis_joints=("j1",), deadzone=0.0)
    source = GamepadEventSource(mapping=mapping)

    async def scenario() -> int:
        # Source never started — feed must not raise nor enqueue.
        await source.feed(GamepadEvent(axes=(0.5,), buttons=()))
        return source._queue.qsize()  # noqa: SLF001

    assert _run(scenario()) == 0


def test_gamepad_source_deadzone_value_is_zero_not_dropped():
    """Deadzone values pass through as 0.0 so a released stick re-centers the joint."""

    mapping = LinearAxisMapping(axis_joints=("j1",), deadzone=0.2)
    source = GamepadEventSource(mapping=mapping)

    async def scenario() -> list[dict[str, float]]:
        await source.start()
        await source.feed(GamepadEvent(axes=(0.1,), buttons=()))  # inside deadzone -> 0
        await source.feed(GamepadEvent(axes=(0.8,), buttons=()))  # rescaled -> 0.75
        collected: list[dict[str, float]] = []
        async for payload in source.events():
            collected.append(payload)
            if len(collected) == 2:
                break
        await source.stop()
        return collected

    got = _run(scenario())
    assert got[0]["j1"] == pytest.approx(0.0)
    assert got[1]["j1"] == pytest.approx(0.75)
