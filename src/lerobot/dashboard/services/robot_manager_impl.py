"""Real :class:`RobotManagerProtocol` implementation backed by lerobot robots.

Swaps in for :class:`InMemoryRobotManager` when ``fake_devices=False``.
Dispatches :class:`RobotEntry` instances to concrete
:class:`lerobot.robots.Robot` subclasses based on ``robot_type``:

* ``so100_follower`` / ``so101_follower`` / ``koch_follower`` — USB-serial
  followers. Port from :class:`SerialConnection`.
* ``ur`` — Universal Robots e-series over RTDE. Host from
  :class:`NetworkConnection`.

The dashboard's :class:`LerobotCameraManager` owns camera lifecycles, so
the robot configs are built with an empty ``cameras`` dict — downstream
consumers (streaming, recording, VLA) compose robot observations with
camera frames themselves. This avoids double-opening the device and keeps
the robot_manager stateless w.r.t. cameras.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

# Importing the UR adapter here registers the ``"ur"`` choice on
# :class:`RobotConfig` before any dispatch call.
import lerobot.dashboard.vendor.ur_rtde_adapter  # noqa: F401
from lerobot.dashboard.services.registry_models import (
    CameraEntry,
    NetworkConnection,
    RobotEntry,
    RobotStatus,
    SerialConnection,
)
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol
from lerobot.robots.config import RobotConfig

if TYPE_CHECKING:  # pragma: no cover
    from lerobot.robots.robot import Robot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


RobotBuilder = Callable[[RobotEntry], "Robot"]


def _require_serial(entry: RobotEntry) -> SerialConnection:
    if not isinstance(entry.connection, SerialConnection):
        raise ValueError(
            f"robot {entry.name!r} ({entry.robot_type}) requires a SerialConnection, "
            f"got {type(entry.connection).__name__}"
        )
    return entry.connection


def _require_network(entry: RobotEntry) -> NetworkConnection:
    if not isinstance(entry.connection, NetworkConnection):
        raise ValueError(
            f"robot {entry.name!r} ({entry.robot_type}) requires a NetworkConnection, "
            f"got {type(entry.connection).__name__}"
        )
    return entry.connection


def _build_so_follower(entry: RobotEntry) -> Robot:
    from lerobot.robots.so_follower.config_so_follower import SOFollowerRobotConfig
    from lerobot.robots.so_follower.so_follower import SOFollower

    conn = _require_serial(entry)
    config = SOFollowerRobotConfig(
        id=str(entry.id),
        port=conn.port,
        cameras={},
        **(dict(conn.model_opts) if conn.model_opts else {}),
    )
    return SOFollower(config)


def _build_koch_follower(entry: RobotEntry) -> Robot:
    from lerobot.robots.koch_follower.config_koch_follower import KochFollowerConfig
    from lerobot.robots.koch_follower.koch_follower import KochFollower

    conn = _require_serial(entry)
    config = KochFollowerConfig(
        id=str(entry.id),
        port=conn.port,
        cameras={},
        **(dict(conn.model_opts) if conn.model_opts else {}),
    )
    return KochFollower(config)


def _build_ur(entry: RobotEntry) -> Robot:
    from lerobot.dashboard.vendor.ur_rtde_adapter import URRobot, URRobotConfig

    conn = _require_network(entry)
    extras: dict[str, Any] = dict(conn.extra) if conn.extra else {}
    config = URRobotConfig(
        id=str(entry.id),
        host=conn.host,
        cameras={},
        **extras,
    )
    return URRobot(config)


_BUILDERS: dict[str, RobotBuilder] = {
    "so100_follower": _build_so_follower,
    "so101_follower": _build_so_follower,
    "koch_follower": _build_koch_follower,
    "ur": _build_ur,
}


def register_robot_builder(robot_type: str, builder: RobotBuilder) -> None:
    """Install ``builder`` for ``robot_type`` at runtime.

    Intended for tests and for third-party robot integrations that want to
    plug in without forking this module.
    """
    _BUILDERS[robot_type] = builder


def build_robot(entry: RobotEntry) -> Robot:
    """Instantiate the lerobot :class:`Robot` matching ``entry.robot_type``.

    Raises :class:`ValueError` for unknown robot types so the HTTP layer
    can surface a 422. Cameras are omitted intentionally — see module
    docstring.
    """
    builder = _BUILDERS.get(entry.robot_type)
    if builder is None:
        # Best-effort: if the type is registered via draccus ChoiceRegistry,
        # at least surface that in the error so operators can register a
        # matching builder without guessing.
        known_registered = [
            name
            for name, cls in getattr(RobotConfig, "get_known_choices", lambda: {})().items()
            if cls is not None
        ]
        raise ValueError(
            f"no robot builder registered for type {entry.robot_type!r}. "
            f"Known builders: {sorted(_BUILDERS)}. "
            f"ChoiceRegistry entries: {known_registered}"
        )
    return builder(entry)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


@dataclass
class _RobotSlot:
    entry: RobotEntry
    robot: Robot
    status: RobotStatus = field(default_factory=RobotStatus)


class LerobotRobotManager:
    """Live :class:`RobotManagerProtocol` implementation.

    Instantiates a :class:`lerobot.robots.Robot` per :class:`RobotEntry`
    and serializes access with an :class:`asyncio.Lock`. Blocking SDK
    calls (connect, disconnect, get_observation, send_action) are offloaded
    to worker threads via :func:`asyncio.to_thread` so the FastAPI event
    loop never stalls.
    """

    def __init__(self, robot_builder: RobotBuilder | None = None) -> None:
        self._lock = asyncio.Lock()
        self._slots: dict[UUID, _RobotSlot] = {}
        self._builder = robot_builder or build_robot

    # ----- lifecycle ------------------------------------------------------

    async def connect(
        self,
        entry: RobotEntry,
        cameras: list[CameraEntry],  # noqa: ARG002 — see docstring
    ) -> RobotStatus:
        """Instantiate and connect the underlying lerobot robot.

        ``cameras`` is accepted for :class:`RobotManagerProtocol` parity but
        intentionally ignored — see module docstring for the split between
        robot_manager and camera_manager.
        """
        async with self._lock:
            slot = self._slots.get(entry.id)
            if slot is not None and slot.status.online:
                return slot.status.model_copy()
        try:
            robot = await asyncio.to_thread(self._builder, entry)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.exception("robot %s build failed", entry.id)
            offline = RobotStatus(online=False, last_error=message)
            async with self._lock:
                if slot is not None:
                    slot.status = offline
            return offline.model_copy()
        async with self._lock:
            slot = _RobotSlot(entry=entry, robot=robot)
            self._slots[entry.id] = slot

        # ``calibrate=False`` — the dashboard never blocks on interactive
        # calibration. Task #15 exposes a dedicated calibration UI that
        # drives the robot-specific routine without a TTY.
        try:
            await asyncio.to_thread(robot.connect, False)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.exception("robot %s connect failed", entry.id)
            async with self._lock:
                slot.status = RobotStatus(online=False, last_error=message)
            return slot.status.model_copy()

        async with self._lock:
            slot.status = RobotStatus(
                online=True,
                last_error=None,
                connected_at=datetime.now(UTC),
            )
            return slot.status.model_copy()

    async def disconnect(self, robot_id: UUID) -> RobotStatus:
        async with self._lock:
            slot = self._slots.get(robot_id)
        if slot is None:
            return RobotStatus(online=False)
        try:
            await asyncio.to_thread(slot.robot.disconnect)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.exception("robot %s disconnect failed", robot_id)
            async with self._lock:
                slot.status = RobotStatus(
                    online=False,
                    last_error=message,
                    connected_at=slot.status.connected_at,
                )
                return slot.status.model_copy()
        async with self._lock:
            slot.status = RobotStatus(
                online=False,
                last_error=slot.status.last_error,
                connected_at=slot.status.connected_at,
            )
            return slot.status.model_copy()

    async def is_connected(self, robot_id: UUID) -> bool:
        async with self._lock:
            slot = self._slots.get(robot_id)
            return bool(slot and slot.status.online)

    async def get_status(self, robot_id: UUID) -> RobotStatus:
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None:
                return RobotStatus()
            return slot.status.model_copy()

    # ----- action / observation ------------------------------------------

    async def send_action(self, robot_id: UUID, action: dict[str, float]) -> None:
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None or not slot.status.online:
                return
            robot = slot.robot
        try:
            await asyncio.to_thread(robot.send_action, action)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.exception("robot %s send_action failed", robot_id)
            async with self._lock:
                slot.status = RobotStatus(
                    online=False,
                    last_error=message,
                    connected_at=slot.status.connected_at,
                )

    async def read_observation(self, robot_id: UUID) -> dict[str, Any]:
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None or not slot.status.online:
                return {}
            robot = slot.robot
        try:
            obs = await asyncio.to_thread(robot.get_observation)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.exception("robot %s get_observation failed", robot_id)
            async with self._lock:
                slot.status = RobotStatus(
                    online=False,
                    last_error=message,
                    connected_at=slot.status.connected_at,
                )
            return {}
        return dict(obs)

    async def read_raw_encoder_ticks(self, robot_id: UUID) -> dict[str, int]:
        """Read raw encoder ticks from the robot's Feetech motor bus.

        Calls ``bus.sync_read("Present_Position", normalize=False)`` to bypass
        the calibration table.  Non-Feetech robots (e.g. UR) return ``{}``
        because they don't expose a ``.bus`` attribute.  Any read failure
        returns ``{}`` and logs a debug message rather than marking the robot
        offline — raw-read failures are softer than ``get_observation`` failures.
        """
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None or not slot.status.online or slot.robot is None:
                return {}
            robot = slot.robot
        bus = getattr(robot, "bus", None)
        if bus is None:
            return {}
        try:
            raw: dict[str, int] = await asyncio.to_thread(bus.sync_read, "Present_Position", normalize=False)
            return {motor: int(tick) for motor, tick in raw.items()}
        except Exception:  # noqa: BLE001
            logger.debug("robot %s raw encoder read failed", robot_id, exc_info=True)
            return {}

    async def get_features(self, robot_id: UUID) -> dict[str, dict]:
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None or slot.robot is None:
                return {"observation": {}, "action": {}}
            robot = slot.robot
        return {
            "observation": dict(getattr(robot, "observation_features", {}) or {}),
            "action": dict(getattr(robot, "action_features", {}) or {}),
        }


# Runtime protocol check — catch signature drift early.
assert isinstance(LerobotRobotManager(), RobotManagerProtocol)


__all__ = [
    "LerobotRobotManager",
    "build_robot",
    "register_robot_builder",
]
