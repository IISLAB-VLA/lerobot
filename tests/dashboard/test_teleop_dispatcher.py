"""Unit tests for :class:`TeleopDispatcher` (task #11 / #12).

Exercises the deadman gate, validator path, and lifecycle semantics
without any hardware or WS transport. Fake sources and a fake robot
manager are defined inline so the test reads as a single contract.

The real :class:`TeleopManagerProtocol.handle_input` aux channel lands
in a follow-up once backend-architect-3's concrete manager is merged.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, TypeVar
from uuid import UUID, uuid4

import pytest

from lerobot.dashboard.services.registry_models import (
    CameraEntry,
    RobotEntry,
    RobotStatus,
    TeleopEntry,
    TeleopStatus,
)
from lerobot.dashboard.teleop import (
    ActionSpec,
    ActionValidator,
    DeadmanState,
    DeadmanStateMachine,
    KeyboardEvent,
    TeleopDispatcher,
    TeleopEvent,
)
from lerobot.dashboard.teleop.adapters.base import SourceState

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRobotManager:
    """Minimal ``RobotManagerProtocol`` — only ``send_action`` is exercised."""

    def __init__(self) -> None:
        self.sent: list[tuple[UUID, dict[str, float]]] = []
        self.fail_next: bool = False

    async def connect(self, entry: RobotEntry, cameras: list[CameraEntry]) -> RobotStatus:
        return RobotStatus(online=True)

    async def disconnect(self, robot_id: UUID) -> RobotStatus:
        return RobotStatus(online=False)

    async def is_connected(self, robot_id: UUID) -> bool:
        return True

    async def get_status(self, robot_id: UUID) -> RobotStatus:
        return RobotStatus(online=True)

    async def send_action(self, robot_id: UUID, action: dict[str, float]) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated robot failure")
        self.sent.append((robot_id, dict(action)))

    async def read_observation(self, robot_id: UUID) -> dict[str, Any]:
        return {}


class FakeTeleopManager:
    """Minimal TeleopManagerProtocol — only handle_input is exercised."""

    def __init__(self) -> None:
        self.received: list[tuple[UUID, TeleopEvent]] = []
        self.fail_next: bool = False

    async def attach(self, entry: TeleopEntry, robot_id: UUID) -> TeleopStatus:
        return TeleopStatus(attached=True)

    async def detach(self, teleop_id: UUID) -> TeleopStatus:
        return TeleopStatus()

    async def is_attached(self, teleop_id: UUID) -> bool:
        return True

    async def get_status(self, teleop_id: UUID) -> TeleopStatus:
        return TeleopStatus(attached=True)

    async def handle_input(self, teleop_id: UUID, event: TeleopEvent) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated teleop failure")
        self.received.append((teleop_id, event))


class ScriptedSource:
    """TeleopEventSource that replays a fixed list of payloads, then stalls."""

    def __init__(self, payloads: list[dict[str, float]]) -> None:
        self._payloads = list(payloads)
        self._done = asyncio.Event()
        self.state: SourceState = SourceState.STOPPED

    async def start(self) -> None:
        self.state = SourceState.RUNNING

    async def stop(self) -> None:
        self.state = SourceState.STOPPED
        self._done.set()

    async def events(self) -> AsyncIterator[dict[str, float]]:
        for payload in self._payloads:
            yield payload
        # Block until stopped so the dispatcher's consumer task stays alive
        # (mirroring how a real source would behave).
        await self._done.wait()


class ClockStub:
    """Monotonic-like clock the dispatcher can read without wall time."""

    def __init__(self, start_s: float = 0.0) -> None:
        self._t = start_s

    def __call__(self) -> float:
        return self._t

    def advance(self, dt_s: float) -> None:
        self._t += dt_s


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _spec() -> ActionSpec:
    return ActionSpec(
        joint_names=("j1", "j2"),
        low=(-1.0, -1.0),
        high=(1.0, 1.0),
        rate_limit_hz=100.0,
    )


def _engaged_deadman(clock: ClockStub) -> DeadmanStateMachine:
    sm = DeadmanStateMachine(arming_delay_ms=0.0, heartbeat_timeout_ms=1_000_000.0)
    sm.press(clock() * 1000)
    sm.tick(clock() * 1000)
    assert sm.state is DeadmanState.ENGAGED
    return sm


async def _drain(dispatcher: TeleopDispatcher) -> None:
    """Let consumer tasks run to completion of their scripted payloads."""
    # A couple of loop turns are enough — the ScriptedSource yields and
    # the validator path is fully synchronous.
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_forward_accepted_payload_when_deadman_engaged():
    clock = ClockStub()
    robot_id = uuid4()
    manager = FakeRobotManager()
    validator = ActionValidator(_spec())
    deadman = _engaged_deadman(clock)
    source = ScriptedSource([{"j1": 0.1, "j2": -0.2}])
    dispatcher = TeleopDispatcher(robot_id, manager, validator, deadman, clock=clock)

    async def scenario() -> None:
        await dispatcher.attach(source)
        await _drain(dispatcher)
        await dispatcher.close()

    _run(scenario())
    assert manager.sent == [(robot_id, {"j1": 0.1, "j2": -0.2})]
    assert dispatcher.forwarded == 1
    assert dispatcher.dropped_deadman == 0


def test_drop_payload_when_deadman_not_engaged():
    clock = ClockStub()
    robot_id = uuid4()
    manager = FakeRobotManager()
    validator = ActionValidator(_spec())
    deadman = DeadmanStateMachine()  # IDLE
    source = ScriptedSource([{"j1": 0.1, "j2": 0.2}, {"j1": 0.3, "j2": 0.4}])
    dispatcher = TeleopDispatcher(robot_id, manager, validator, deadman, clock=clock)

    async def scenario() -> None:
        await dispatcher.attach(source)
        await _drain(dispatcher)
        await dispatcher.close()

    _run(scenario())
    assert manager.sent == []
    assert dispatcher.dropped_deadman == 2
    assert dispatcher.forwarded == 0


def test_drop_payload_on_validation_failure():
    # Use _process directly so the clock can be advanced deterministically
    # between payloads — ScriptedSource would otherwise hand all three to
    # the consumer inside one loop turn and the second accept would be
    # rejected as a rate-limit burst instead of the range check we want.
    clock = ClockStub()
    robot_id = uuid4()
    manager = FakeRobotManager()
    validator = ActionValidator(_spec())
    deadman = _engaged_deadman(clock)
    dispatcher = TeleopDispatcher(robot_id, manager, validator, deadman, clock=clock)

    async def scenario() -> None:
        await dispatcher._process({"j1": 0.1, "j2": 0.0})  # accepted
        clock.advance(0.020)
        await dispatcher._process({"j1": 5.0, "j2": 0.0})  # out_of_range
        clock.advance(0.020)
        await dispatcher._process({"j1": 0.2, "j2": 0.0})  # accepted
        await dispatcher.close()

    _run(scenario())
    forwarded_values = [a["j1"] for _, a in manager.sent]
    assert forwarded_values == [0.1, 0.2]
    assert dispatcher.dropped_validation == 1


def test_validator_clock_reset_when_deadman_disengages():
    """Re-engaging deadman after a gap must not trip the burst guard."""

    clock = ClockStub()
    robot_id = uuid4()
    manager = FakeRobotManager()
    validator = ActionValidator(_spec())
    deadman = _engaged_deadman(clock)
    dispatcher = TeleopDispatcher(robot_id, manager, validator, deadman, clock=clock)

    async def scenario() -> None:
        await dispatcher._process({"j1": 0.0, "j2": 0.0})
        # Disengage deadman; a stale validator clock would now live in the
        # past. Dispatcher must call validator.reset() each time it drops.
        deadman.release(clock() * 1000)
        await dispatcher._process({"j1": 0.1, "j2": 0.1})  # dropped
        # Re-engage deadman and send again; no rate_limit rejection should
        # fire even though the last accepted timestamp was long ago.
        deadman.press(clock() * 1000)
        deadman.tick(clock() * 1000)
        clock.advance(0.020)
        await dispatcher._process({"j1": 0.2, "j2": 0.2})
        await dispatcher.close()

    _run(scenario())
    assert [a["j1"] for _, a in manager.sent] == [0.0, 0.2]
    assert dispatcher.dropped_deadman == 1
    assert dispatcher.dropped_validation == 0


def test_send_action_exception_counts_as_drop():
    clock = ClockStub()
    robot_id = uuid4()
    manager = FakeRobotManager()
    manager.fail_next = True
    validator = ActionValidator(_spec())
    deadman = _engaged_deadman(clock)
    dispatcher = TeleopDispatcher(robot_id, manager, validator, deadman, clock=clock)

    async def scenario() -> None:
        await dispatcher._process({"j1": 0.0, "j2": 0.0})  # first call raises
        clock.advance(0.020)
        await dispatcher._process({"j1": 0.1, "j2": 0.1})  # succeeds
        await dispatcher.close()

    _run(scenario())
    assert manager.sent == [(robot_id, {"j1": 0.1, "j2": 0.1})]
    assert dispatcher.forwarded == 1
    assert dispatcher.dropped_validation == 1


def test_attach_starts_source_and_close_stops_it():
    clock = ClockStub()
    manager = FakeRobotManager()
    validator = ActionValidator(_spec())
    deadman = _engaged_deadman(clock)
    source = ScriptedSource([])
    dispatcher = TeleopDispatcher(uuid4(), manager, validator, deadman, clock=clock)

    async def scenario() -> tuple[SourceState, SourceState]:
        await dispatcher.attach(source)
        started = source.state
        await dispatcher.close()
        return started, source.state

    started, stopped = _run(scenario())
    assert started is SourceState.RUNNING
    assert stopped is SourceState.STOPPED


def test_handle_event_forwards_to_teleop_manager_when_wired():
    clock = ClockStub()
    manager = FakeTeleopManager()
    teleop_id = uuid4()
    dispatcher = TeleopDispatcher(
        uuid4(),
        FakeRobotManager(),
        ActionValidator(_spec()),
        DeadmanStateMachine(),
        clock=clock,
        teleop_manager=manager,
        teleop_id=teleop_id,
    )

    async def scenario() -> None:
        await dispatcher.handle_event(KeyboardEvent(key="w", pressed=True))
        await dispatcher.handle_event(KeyboardEvent(key="w", pressed=False))
        await dispatcher.close()

    _run(scenario())
    assert [e.key for _, e in manager.received] == ["w", "w"]
    assert [e.pressed for _, e in manager.received] == [True, False]
    assert all(tid == teleop_id for tid, _ in manager.received)
    assert dispatcher.aux_events_forwarded == 2
    assert dispatcher.aux_events_dropped == 0


def test_handle_event_is_noop_without_teleop_manager():
    dispatcher = TeleopDispatcher(
        uuid4(),
        FakeRobotManager(),
        ActionValidator(_spec()),
        DeadmanStateMachine(),
    )

    async def scenario() -> None:
        await dispatcher.handle_event(KeyboardEvent(key="x", pressed=True))
        await dispatcher.close()

    _run(scenario())
    assert dispatcher.aux_events_forwarded == 0
    assert dispatcher.aux_events_dropped == 1


def test_handle_event_drops_on_manager_failure():
    manager = FakeTeleopManager()
    manager.fail_next = True
    dispatcher = TeleopDispatcher(
        uuid4(),
        FakeRobotManager(),
        ActionValidator(_spec()),
        DeadmanStateMachine(),
        teleop_manager=manager,
        teleop_id=uuid4(),
    )

    async def scenario() -> None:
        await dispatcher.handle_event(KeyboardEvent(key="w", pressed=True))  # first raises
        await dispatcher.handle_event(KeyboardEvent(key="w", pressed=False))  # succeeds
        await dispatcher.close()

    _run(scenario())
    assert len(manager.received) == 1
    assert dispatcher.aux_events_forwarded == 1
    assert dispatcher.aux_events_dropped == 1


def test_aux_channel_requires_both_manager_and_id():
    with pytest.raises(ValueError):
        TeleopDispatcher(
            uuid4(),
            FakeRobotManager(),
            ActionValidator(_spec()),
            DeadmanStateMachine(),
            teleop_manager=FakeTeleopManager(),
        )
    with pytest.raises(ValueError):
        TeleopDispatcher(
            uuid4(),
            FakeRobotManager(),
            ActionValidator(_spec()),
            DeadmanStateMachine(),
            teleop_id=uuid4(),
        )


def test_close_is_idempotent_and_blocks_further_attach():
    clock = ClockStub()
    dispatcher = TeleopDispatcher(
        uuid4(),
        FakeRobotManager(),
        ActionValidator(_spec()),
        _engaged_deadman(clock),
        clock=clock,
    )

    async def scenario() -> None:
        await dispatcher.close()
        await dispatcher.close()  # idempotent
        with pytest.raises(RuntimeError):
            await dispatcher.attach(ScriptedSource([]))

    _run(scenario())
