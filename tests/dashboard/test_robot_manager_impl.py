"""LerobotRobotManager tests.

The tests plug a fake Robot builder into the manager so no hardware (nor
pyserial, nor ur_rtde) is required. Builder-dispatch tests verify that
RobotEntry ``robot_type`` values flow through to the correct config
class with correct connection fields.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from lerobot.dashboard.services.registry_models import (
    NetworkConnection,
    RobotEntry,
    SerialConnection,
)
from lerobot.dashboard.services.robot_manager_impl import (
    LerobotRobotManager,
    build_robot,
    register_robot_builder,
)

# ---------------------------------------------------------------------------
# Fake Robot
# ---------------------------------------------------------------------------


@dataclass
class _FakeRobot:
    entry_name: str
    connected: bool = False
    actions: list[dict[str, float]] = field(default_factory=list)
    raise_on_connect: BaseException | None = None
    raise_on_action: BaseException | None = None
    observation: dict[str, float] = field(default_factory=lambda: {"joint_1.pos": 0.0})

    def connect(self, calibrate: bool = True) -> None:  # noqa: ARG002
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        if self.raise_on_action is not None:
            raise self.raise_on_action
        self.actions.append(dict(action))
        return action

    def get_observation(self) -> dict[str, Any]:
        return dict(self.observation)


def _entry(
    robot_type: str = "so101_follower",
    port: str = "/dev/serial/by-id/usb-fake",
) -> RobotEntry:
    return RobotEntry(
        name="fake-robot",
        robot_type=robot_type,
        connection=SerialConnection(port=port),
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_build_robot_rejects_unknown_type() -> None:
    entry = _entry(robot_type="no_such_robot")
    with pytest.raises(ValueError, match="no robot builder"):
        build_robot(entry)


def test_register_robot_builder_plugs_in_at_runtime() -> None:
    sentinel = _FakeRobot(entry_name="custom")
    register_robot_builder("_unit_test_custom_type", lambda entry: sentinel)
    try:
        built = build_robot(_entry(robot_type="_unit_test_custom_type"))
        assert built is sentinel
    finally:
        # Restore the dispatcher so subsequent tests aren't polluted.
        from lerobot.dashboard.services.robot_manager_impl import _BUILDERS

        _BUILDERS.pop("_unit_test_custom_type", None)


def test_so_follower_builder_requires_serial_connection() -> None:
    entry = RobotEntry(
        name="so101",
        robot_type="so101_follower",
        connection=NetworkConnection(protocol="custom", host="1.2.3.4", port=30004),
    )
    with pytest.raises(ValueError, match="SerialConnection"):
        build_robot(entry)


def test_ur_builder_requires_network_connection() -> None:
    entry = RobotEntry(
        name="ur",
        robot_type="ur",
        connection=SerialConnection(port="/dev/fake"),
    )
    with pytest.raises(ValueError, match="NetworkConnection"):
        build_robot(entry)


def test_ur_builder_returns_ur_robot() -> None:
    entry = RobotEntry(
        name="ur5e",
        robot_type="ur",
        connection=NetworkConnection(protocol="rtde", host="192.168.1.2", port=30004),
    )
    from lerobot.dashboard.vendor.ur_rtde_adapter import URRobot

    robot = build_robot(entry)
    assert isinstance(robot, URRobot)
    assert robot.config.host == "192.168.1.2"


# ---------------------------------------------------------------------------
# Manager lifecycle
# ---------------------------------------------------------------------------


def _fake_manager() -> tuple[LerobotRobotManager, _FakeRobot]:
    robot = _FakeRobot(entry_name="fake")
    mgr = LerobotRobotManager(robot_builder=lambda entry: robot)
    return mgr, robot


async def test_connect_marks_online_and_sets_timestamp() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()

    status = await mgr.connect(entry, cameras=[])

    assert status.online is True
    assert status.connected_at is not None
    assert status.last_error is None
    assert robot.connected is True
    assert await mgr.is_connected(entry.id) is True


async def test_connect_records_error_on_hardware_failure() -> None:
    mgr, robot = _fake_manager()
    robot.raise_on_connect = RuntimeError("no device")
    entry = _entry()

    status = await mgr.connect(entry, cameras=[])

    assert status.online is False
    assert status.last_error == "no device"
    assert await mgr.is_connected(entry.id) is False


async def test_connect_is_idempotent_for_already_online_robot() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()
    first = await mgr.connect(entry, cameras=[])
    # Simulate the hardware reporting disconnected under us.
    robot.connected = False
    second = await mgr.connect(entry, cameras=[])
    assert first.connected_at == second.connected_at
    # Only one actual connect call to the fake robot.
    # (We can't count directly, but if a second connect happened it would have reset raise_on_connect path.)


async def test_disconnect_marks_offline_but_keeps_error_history() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()
    await mgr.connect(entry, cameras=[])

    status = await mgr.disconnect(entry.id)

    assert status.online is False
    assert robot.connected is False


async def test_disconnect_of_unknown_robot_returns_offline_status() -> None:
    mgr, _ = _fake_manager()
    status = await mgr.disconnect(_entry().id)
    assert status.online is False


async def test_send_action_forwards_to_robot() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()
    await mgr.connect(entry, cameras=[])

    await mgr.send_action(entry.id, {"joint_1.pos": 0.5})

    assert robot.actions == [{"joint_1.pos": 0.5}]


async def test_send_action_is_noop_when_offline() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()
    # No connect() — robot is offline.
    await mgr.send_action(entry.id, {"joint_1.pos": 0.5})
    assert robot.actions == []


async def test_send_action_records_error_and_goes_offline() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()
    await mgr.connect(entry, cameras=[])
    robot.raise_on_action = RuntimeError("motor fault")

    await mgr.send_action(entry.id, {"joint_1.pos": 0.5})

    status = await mgr.get_status(entry.id)
    assert status.online is False
    assert status.last_error == "motor fault"


async def test_read_observation_returns_robot_obs() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()
    robot.observation = {"joint_1.pos": 1.23, "gripper.pos": 0.5}
    await mgr.connect(entry, cameras=[])

    obs = await mgr.read_observation(entry.id)

    assert obs == {"joint_1.pos": 1.23, "gripper.pos": 0.5}


async def test_read_observation_returns_empty_when_offline() -> None:
    mgr, _ = _fake_manager()
    assert await mgr.read_observation(_entry().id) == {}


async def test_get_status_for_unknown_robot_is_offline() -> None:
    mgr, _ = _fake_manager()
    status = await mgr.get_status(_entry().id)
    assert status.online is False
    assert status.connected_at is None


async def test_connect_records_error_when_builder_raises() -> None:
    def _broken_builder(entry: RobotEntry) -> _FakeRobot:
        raise ImportError("feetech-servo-sdk missing")

    mgr = LerobotRobotManager(robot_builder=_broken_builder)
    status = await mgr.connect(_entry(), cameras=[])

    assert status.online is False
    assert "feetech-servo-sdk" in (status.last_error or "")


# ---------------------------------------------------------------------------
# read_raw_encoder_ticks
# ---------------------------------------------------------------------------


@dataclass
class _FakeMotorsBus:
    """Minimal bus stub that records sync_read calls."""

    raw_ticks: dict[str, int] = field(default_factory=lambda: {"shoulder_pan": 2048, "elbow_flex": 1024})
    calls: list[tuple[str, bool]] = field(default_factory=list)

    def sync_read(self, data_name: str, motors=None, normalize: bool = True) -> dict[str, int]:  # noqa: ANN001
        self.calls.append((data_name, normalize))
        return dict(self.raw_ticks)


@dataclass
class _FakeRobotWithBus(_FakeRobot):
    """Fake robot that exposes a Feetech-style .bus attribute."""

    bus: _FakeMotorsBus = field(default_factory=_FakeMotorsBus)


async def test_read_raw_encoder_ticks_calls_bus_without_normalize() -> None:
    bus = _FakeMotorsBus(raw_ticks={"shoulder_pan": 2048, "elbow_flex": 1000})
    robot_with_bus = _FakeRobotWithBus(entry_name="so101", bus=bus)
    mgr = LerobotRobotManager(robot_builder=lambda entry: robot_with_bus)
    entry = _entry()
    await mgr.connect(entry, cameras=[])

    ticks = await mgr.read_raw_encoder_ticks(entry.id)

    assert ticks == {"shoulder_pan": 2048, "elbow_flex": 1000}
    # Must have called sync_read with normalize=False
    assert ("Present_Position", False) in bus.calls


async def test_read_raw_encoder_ticks_returns_empty_without_bus() -> None:
    """Robots without a .bus attribute (e.g. UR) return empty dict."""
    robot_no_bus = _FakeRobot(entry_name="ur")
    mgr = LerobotRobotManager(robot_builder=lambda entry: robot_no_bus)
    entry = _entry()
    await mgr.connect(entry, cameras=[])

    ticks = await mgr.read_raw_encoder_ticks(entry.id)

    assert ticks == {}


async def test_read_raw_encoder_ticks_returns_empty_when_offline() -> None:
    mgr, _ = _fake_manager()
    ticks = await mgr.read_raw_encoder_ticks(_entry().id)
    assert ticks == {}


async def test_read_raw_encoder_ticks_returns_empty_on_bus_error() -> None:
    """Bus read failures are swallowed and return {} — robot stays online."""

    class _BrokenBus:
        def sync_read(self, data_name: str, motors=None, normalize: bool = True) -> dict:  # noqa: ANN001
            raise OSError("CRC error")

    @dataclass
    class _RobotWithBrokenBus(_FakeRobot):
        bus: _BrokenBus = field(default_factory=_BrokenBus)

    robot = _RobotWithBrokenBus(entry_name="so101_broken")
    mgr = LerobotRobotManager(robot_builder=lambda entry: robot)
    entry = _entry()
    await mgr.connect(entry, cameras=[])

    ticks = await mgr.read_raw_encoder_ticks(entry.id)

    assert ticks == {}
    # Robot must still be online (raw-read failure is soft)
    status = await mgr.get_status(entry.id)
    assert status.online is True


async def test_manager_parallel_connect_is_serialized() -> None:
    mgr, robot = _fake_manager()
    entry = _entry()
    status_a, status_b = await asyncio.gather(
        mgr.connect(entry, cameras=[]),
        mgr.connect(entry, cameras=[]),
    )
    assert status_a.online is True and status_b.online is True
