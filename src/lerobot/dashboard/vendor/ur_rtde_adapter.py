"""Universal Robots RTDE adapter for the lerobot :class:`Robot` interface.

Wraps the `ur_rtde <https://sdurobotics.gitlab.io/ur_rtde/>`_ bindings so
Universal Robots e-series arms (UR3e/UR5e/UR7e/UR10e/UR16e/UR20/UR30) can
flow through the same dashboard pipeline as the SO follower family — the
dashboard's :class:`RobotManagerProtocol` implementation instantiates this
from a :class:`RobotEntry` with a ``NetworkConnection``.

Design
------
* ``ur_rtde`` is an optional heavy dependency. It is only imported inside
  :meth:`URRobot.connect`, mirroring the lazy-import pattern used elsewhere
  in lerobot for SDK-gated backends.
* Observations carry the 6 joint angles (``joint_<n>.pos``) plus the 6D TCP
  pose (``tcp.<x|y|z|rx|ry|rz>``) plus any RGB camera frames declared on the
  config. This matches the convention of the Feetech-based SO follower and
  keeps the dataset schema homogeneous.
* Actions are joint-space ``servoJ`` commands. Speed/time parameters live on
  the config so policies can be tuned without touching adapter code.
* Cameras piggy-back on :func:`lerobot.cameras.make_cameras_from_configs` so
  the dashboard can reuse the registry's ``CameraEntry`` without a special
  case for UR hardware.

The adapter deliberately does *not* implement calibration — UR arms come
factory-calibrated and the UR Teach Pendant owns end-effector/TCP
configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

import numpy as np

from lerobot.cameras import CameraConfig, make_cameras_from_configs
from lerobot.robots.config import RobotConfig
from lerobot.robots.robot import Robot
from lerobot.types import RobotAction, RobotObservation

if TYPE_CHECKING:  # pragma: no cover — import-time only
    import rtde_control
    import rtde_receive

logger = logging.getLogger(__name__)


UR_JOINT_NAMES: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow",
    "wrist_1",
    "wrist_2",
    "wrist_3",
)

UR_TCP_AXES: tuple[str, ...] = ("x", "y", "z", "rx", "ry", "rz")


@RobotConfig.register_subclass("ur")
@dataclass
class URRobotConfig(RobotConfig):
    """Configuration for a UR e-series arm driven over RTDE."""

    # IP / hostname of the UR controller.
    host: str = "127.0.0.1"

    # RTDE communication frequency. The UR controller caps this at 500 Hz
    # for e-series; we default to a safe-for-dashboard 125 Hz.
    frequency: float = 125.0

    # servoJ defaults. Documented on the UR RTDE guide:
    #   time:     duration of the motion segment in seconds
    #   lookahead: smoothing window in seconds
    #   gain:     proportional gain for the servoJ controller
    servo_time: float = 0.008
    servo_lookahead: float = 0.1
    servo_gain: int = 300

    # Safety: maximum absolute joint-angle delta allowed per send_action call.
    # ``None`` disables the clamp — use only in trusted research contexts.
    max_relative_target: float | None = 0.1

    # Connect flags forwarded to ``RTDEControlInterface``.
    rt_priority: bool = False
    verbose: bool = False

    # Optional RGB cameras published into the observation dict alongside joints.
    cameras: dict[str, CameraConfig] = field(default_factory=dict)


class URRobot(Robot):
    """Universal Robots RTDE-backed :class:`Robot`.

    The class is importable even when ``ur_rtde`` is missing — the
    dependency is resolved lazily inside :meth:`connect`. This keeps the
    module safe to import in CI environments without the UR SDK.
    """

    config_class = URRobotConfig
    name = "ur"

    def __init__(self, config: URRobotConfig) -> None:
        super().__init__(config)
        self.config = config
        self.cameras = make_cameras_from_configs(config.cameras)
        self._rtde_control: rtde_control.RTDEControlInterface | None = None
        self._rtde_receive: rtde_receive.RTDEReceiveInterface | None = None
        self._last_target: list[float] | None = None

    # ----- feature dicts -------------------------------------------------

    @property
    def _joints_ft(self) -> dict[str, type]:
        return {f"{j}.pos": float for j in UR_JOINT_NAMES}

    @property
    def _tcp_ft(self) -> dict[str, type]:
        return {f"tcp.{a}": float for a in UR_TCP_AXES}

    @property
    def _cameras_ft(self) -> dict[str, tuple[int, int, int]]:
        return {
            cam: (self.config.cameras[cam].height, self.config.cameras[cam].width, 3)
            for cam in self.cameras
        }

    @cached_property
    def observation_features(self) -> dict[str, type | tuple[int, int, int]]:
        return {**self._joints_ft, **self._tcp_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return self._joints_ft

    # ----- connection -----------------------------------------------------

    @property
    def is_connected(self) -> bool:
        if self._rtde_control is None or self._rtde_receive is None:
            return False
        try:
            return bool(self._rtde_control.isConnected()) and bool(
                self._rtde_receive.isConnected()
            )
        except Exception:  # pragma: no cover — defensive against SDK glitches
            return False

    @property
    def is_calibrated(self) -> bool:
        # UR arms are factory-calibrated; nothing for the dashboard to do.
        return True

    def calibrate(self) -> None:
        """No-op. See class docstring."""

    def configure(self) -> None:
        """Configure the RTDE control interface.

        Called from :meth:`connect` once the interfaces are alive. Left as
        a public hook so policies can subclass and adjust (e.g. set TCP
        offsets or payload before servoing).
        """

    def connect(self, calibrate: bool = True) -> None:  # noqa: ARG002 — not applicable for UR
        if self.is_connected:
            logger.debug("URRobot(%s) already connected", self.id)
            return

        # Lazy import — keeps the module importable without the SDK.
        try:
            import rtde_control as _rtde_control
            import rtde_receive as _rtde_receive
        except ImportError as exc:  # pragma: no cover — env-dependent
            raise RuntimeError(
                "ur-rtde is not installed. Install the 'dashboard' extra: "
                "`uv sync --extra dashboard`."
            ) from exc

        flags = 0
        if self.config.rt_priority:
            flags |= _rtde_control.RTDEControlInterface.FLAG_VERBOSE  # type: ignore[attr-defined]
        if self.config.verbose:
            flags |= _rtde_control.RTDEControlInterface.FLAG_VERBOSE  # type: ignore[attr-defined]

        logger.info("URRobot(%s) connecting to %s @ %sHz", self.id, self.config.host, self.config.frequency)
        self._rtde_control = _rtde_control.RTDEControlInterface(
            self.config.host,
            self.config.frequency,
            flags,
        )
        self._rtde_receive = _rtde_receive.RTDEReceiveInterface(
            self.config.host,
            self.config.frequency,
        )
        for cam in self.cameras.values():
            cam.connect()
        self.configure()

    def disconnect(self) -> None:
        if self._rtde_control is not None:
            try:
                self._rtde_control.stopScript()
            except Exception:  # pragma: no cover
                logger.exception("stopScript failed for UR(%s)", self.id)
            try:
                self._rtde_control.disconnect()
            except Exception:  # pragma: no cover
                logger.exception("rtde_control.disconnect failed for UR(%s)", self.id)
            self._rtde_control = None
        if self._rtde_receive is not None:
            try:
                self._rtde_receive.disconnect()
            except Exception:  # pragma: no cover
                logger.exception("rtde_receive.disconnect failed for UR(%s)", self.id)
            self._rtde_receive = None
        for cam in self.cameras.values():
            try:
                cam.disconnect()
            except Exception:  # pragma: no cover
                logger.exception("camera disconnect failed for UR(%s)", self.id)

    # ----- observation / action ------------------------------------------

    def get_observation(self) -> RobotObservation:
        if not self.is_connected:
            raise RuntimeError(f"URRobot({self.id}) is not connected")
        assert self._rtde_receive is not None  # for type checkers

        joints = self._rtde_receive.getActualQ()
        tcp = self._rtde_receive.getActualTCPPose()
        obs: dict[str, Any] = {}
        for name, value in zip(UR_JOINT_NAMES, joints, strict=True):
            obs[f"{name}.pos"] = float(value)
        for axis, value in zip(UR_TCP_AXES, tcp, strict=True):
            obs[f"tcp.{axis}"] = float(value)
        for name, cam in self.cameras.items():
            obs[name] = cam.async_read() if hasattr(cam, "async_read") else cam.read()
        return obs  # type: ignore[return-value]

    def send_action(self, action: RobotAction) -> RobotAction:
        if not self.is_connected:
            raise RuntimeError(f"URRobot({self.id}) is not connected")
        assert self._rtde_control is not None

        target = [float(action[f"{j}.pos"]) for j in UR_JOINT_NAMES]
        clamped = self._apply_safety_clamp(target)
        self._rtde_control.servoJ(
            clamped,
            0.0,
            0.0,
            self.config.servo_time,
            self.config.servo_lookahead,
            self.config.servo_gain,
        )
        self._last_target = clamped
        return {f"{j}.pos": v for j, v in zip(UR_JOINT_NAMES, clamped, strict=True)}

    # ----- internals ------------------------------------------------------

    def _apply_safety_clamp(self, target: list[float]) -> list[float]:
        limit = self.config.max_relative_target
        if limit is None:
            return target
        current = self._last_target
        if current is None and self._rtde_receive is not None:
            current = list(self._rtde_receive.getActualQ())
        if current is None:
            return target
        clamped = np.clip(
            np.asarray(target) - np.asarray(current),
            -limit,
            limit,
        )
        return (np.asarray(current) + clamped).tolist()


__all__ = ["URRobot", "URRobotConfig", "UR_JOINT_NAMES", "UR_TCP_AXES"]
