"""URRobot adapter tests.

Covers the dashboard's UR e-series adapter without requiring the real
``ur_rtde`` SDK to talk to actual hardware — two fake SDK modules are
installed into ``sys.modules`` ahead of ``URRobot.connect()`` so the lazy
import inside ``connect`` binds to them.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

from lerobot.dashboard.vendor.ur_rtde_adapter import (
    UR_JOINT_NAMES,
    UR_TCP_AXES,
    URRobot,
    URRobotConfig,
)


# ---------------------------------------------------------------------------
# Fake ur_rtde SDK
# ---------------------------------------------------------------------------


class _FakeControl:
    FLAG_VERBOSE = 1
    FLAG_USE_EXT_UR_CAP = 2

    def __init__(self, host: str, frequency: float, flags: int = 0) -> None:
        self.host = host
        self.frequency = frequency
        self.flags = flags
        self.connected = True
        self.servoJ_calls: list[tuple[list[float], float, float, float, float, int]] = []
        self.stopped = False

    def isConnected(self) -> bool:  # noqa: N802 — matches SDK
        return self.connected

    def servoJ(  # noqa: N802 — matches SDK
        self,
        q: list[float],
        speed: float,
        acceleration: float,
        time: float,
        lookahead: float,
        gain: int,
    ) -> None:
        self.servoJ_calls.append((list(q), speed, acceleration, time, lookahead, gain))

    def stopScript(self) -> None:  # noqa: N802 — matches SDK
        self.stopped = True

    def disconnect(self) -> None:
        self.connected = False


class _FakeReceive:
    def __init__(self, host: str, frequency: float) -> None:
        self.host = host
        self.frequency = frequency
        self.connected = True
        self._q = [0.1, -0.2, 0.3, 0.4, -0.5, 0.6]
        self._tcp = [0.4, 0.1, 0.3, 3.14, 0.0, 0.0]

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def getActualQ(self) -> list[float]:  # noqa: N802
        return list(self._q)

    def getActualTCPPose(self) -> list[float]:  # noqa: N802
        return list(self._tcp)

    def disconnect(self) -> None:
        self.connected = False


@pytest.fixture
def fake_rtde(monkeypatch: pytest.MonkeyPatch) -> tuple[type, type]:
    """Install fake ur_rtde modules into ``sys.modules``."""
    control_mod = types.ModuleType("rtde_control")
    control_mod.RTDEControlInterface = _FakeControl  # type: ignore[attr-defined]
    receive_mod = types.ModuleType("rtde_receive")
    receive_mod.RTDEReceiveInterface = _FakeReceive  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rtde_control", control_mod)
    monkeypatch.setitem(sys.modules, "rtde_receive", receive_mod)
    return _FakeControl, _FakeReceive


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_config_registers_as_ur_choice() -> None:
    """The config must be discoverable via ``RobotConfig.get_choice_class('ur')``."""
    from lerobot.robots.config import RobotConfig

    cfg = URRobotConfig(host="10.0.0.1")
    assert cfg.type == "ur"
    assert RobotConfig.get_choice_class("ur") is URRobotConfig


def test_observation_and_action_features_stable() -> None:
    cfg = URRobotConfig(host="10.0.0.1")
    robot = URRobot(cfg)
    obs_ft = robot.observation_features
    act_ft = robot.action_features

    assert set(act_ft) == {f"{j}.pos" for j in UR_JOINT_NAMES}
    for joint in UR_JOINT_NAMES:
        assert obs_ft[f"{joint}.pos"] is float
    for axis in UR_TCP_AXES:
        assert obs_ft[f"tcp.{axis}"] is float


def test_connect_and_disconnect_lazy_loads_rtde(fake_rtde: Any) -> None:
    cfg = URRobotConfig(host="10.1.2.3", frequency=100.0)
    robot = URRobot(cfg)

    assert robot.is_connected is False
    robot.connect()
    assert robot.is_connected is True
    # Host + frequency propagated into the SDK constructors.
    assert robot._rtde_control.host == "10.1.2.3"  # type: ignore[union-attr]
    assert robot._rtde_receive.frequency == 100.0  # type: ignore[union-attr]

    robot.disconnect()
    assert robot.is_connected is False
    assert robot._rtde_control is None
    assert robot._rtde_receive is None


def test_connect_without_sdk_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Block imports by putting None in sys.modules so ``import rtde_control`` fails.
    monkeypatch.setitem(sys.modules, "rtde_control", None)
    monkeypatch.setitem(sys.modules, "rtde_receive", None)
    cfg = URRobotConfig(host="127.0.0.1")
    robot = URRobot(cfg)
    with pytest.raises(RuntimeError, match="ur-rtde is not installed"):
        robot.connect()


def test_get_observation_returns_joint_and_tcp(fake_rtde: Any) -> None:
    robot = URRobot(URRobotConfig(host="127.0.0.1"))
    robot.connect()
    obs = robot.get_observation()
    for joint, expected in zip(UR_JOINT_NAMES, [0.1, -0.2, 0.3, 0.4, -0.5, 0.6], strict=True):
        assert obs[f"{joint}.pos"] == pytest.approx(expected)
    for axis, expected in zip(UR_TCP_AXES, [0.4, 0.1, 0.3, 3.14, 0.0, 0.0], strict=True):
        assert obs[f"tcp.{axis}"] == pytest.approx(expected)


def test_send_action_issues_servoJ_and_reports_target(fake_rtde: Any) -> None:
    cfg = URRobotConfig(host="127.0.0.1", max_relative_target=None)
    robot = URRobot(cfg)
    robot.connect()

    target = {f"{j}.pos": v for j, v in zip(UR_JOINT_NAMES, [0.5, 0.0, 0.0, 0.0, 0.0, 0.0], strict=True)}
    echoed = robot.send_action(target)

    ctrl = robot._rtde_control
    assert ctrl is not None
    assert len(ctrl.servoJ_calls) == 1  # type: ignore[attr-defined]
    q, _, _, t, la, g = ctrl.servoJ_calls[0]  # type: ignore[attr-defined]
    np.testing.assert_allclose(q, [0.5, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert t == cfg.servo_time
    assert la == cfg.servo_lookahead
    assert g == cfg.servo_gain
    assert echoed == target


def test_send_action_clamps_to_max_relative_target(fake_rtde: Any) -> None:
    cfg = URRobotConfig(host="127.0.0.1", max_relative_target=0.05)
    robot = URRobot(cfg)
    robot.connect()

    # Fake starts at [0.1, -0.2, 0.3, 0.4, -0.5, 0.6] — request +1 rad on joint 0.
    target = {f"{j}.pos": v for j, v in zip(UR_JOINT_NAMES, [1.1, -0.2, 0.3, 0.4, -0.5, 0.6], strict=True)}
    echoed = robot.send_action(target)

    # Clamp keeps the delta ≤ 0.05 rad, so joint 0 should be at 0.15 not 1.1.
    assert echoed[f"{UR_JOINT_NAMES[0]}.pos"] == pytest.approx(0.15)


def test_send_action_when_disconnected_raises() -> None:
    robot = URRobot(URRobotConfig(host="127.0.0.1"))
    with pytest.raises(RuntimeError, match="not connected"):
        robot.send_action({f"{j}.pos": 0.0 for j in UR_JOINT_NAMES})


def test_get_observation_when_disconnected_raises() -> None:
    robot = URRobot(URRobotConfig(host="127.0.0.1"))
    with pytest.raises(RuntimeError, match="not connected"):
        robot.get_observation()


def test_disconnect_is_idempotent(fake_rtde: Any) -> None:
    robot = URRobot(URRobotConfig(host="127.0.0.1"))
    robot.connect()
    robot.disconnect()
    robot.disconnect()  # second call must not raise
    assert robot.is_connected is False
