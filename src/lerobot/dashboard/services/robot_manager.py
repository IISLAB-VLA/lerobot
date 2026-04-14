"""Protocol and in-memory fallback for the live robot manager.

The HTTP layer (``/api/robots/{id}/connect`` etc.) must not import from
:mod:`lerobot.robots` directly — many concrete robot backends pull in heavy
hardware dependencies (feetech, dynamixel, ur-rtde, realsense SDK, …) that
are only installed behind extras.

To keep the backend importable in every environment — including CI runners
without hardware — we declare :class:`RobotManagerProtocol` here and lazy-
bind to the real adapter (Task #6, owned by robotics-integrator) at
startup. When no real adapter is wired up, :class:`InMemoryRobotManager`
serves as a no-op stand-in that is sufficient for unit tests and for the
``fake_devices`` mode flag.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from lerobot.dashboard.services.registry_models import (
    CameraEntry,
    RobotEntry,
    RobotStatus,
)


@runtime_checkable
class RobotManagerProtocol(Protocol):
    """Live runtime for dashboard-declared robots.

    Implementations are responsible for instantiating the underlying
    lerobot :class:`Robot` from a :class:`RobotEntry`, wiring up cameras,
    and exposing observation/action hooks to streaming + teleop layers.
    """

    async def connect(self, entry: RobotEntry, cameras: list[CameraEntry]) -> RobotStatus:
        """Instantiate hardware handles for ``entry`` and mark it online."""

    async def disconnect(self, robot_id: UUID) -> RobotStatus:
        """Tear down handles and mark the robot offline."""

    async def is_connected(self, robot_id: UUID) -> bool:
        """Return ``True`` if ``connect`` has succeeded and no error since."""

    async def get_status(self, robot_id: UUID) -> RobotStatus:
        """Return the most recent runtime status for ``robot_id``."""

    async def send_action(self, robot_id: UUID, action: dict[str, float]) -> None:
        """Forward an action to the live robot. No-ops when offline."""

    async def read_observation(self, robot_id: UUID) -> dict[str, Any]:
        """Read the latest observation (joint positions, camera frames, …)."""

    async def get_features(self, robot_id: UUID) -> dict[str, dict]:
        """Return the hardware feature dicts for the attached :class:`Robot`.

        The result has the form ``{"observation": {...}, "action": {...}}``
        using the same ``float | tuple`` value convention as
        :pyattr:`Robot.observation_features` / :pyattr:`Robot.action_features`.
        Downstream consumers (dataset recorder, VLA inference) feed this
        into :func:`lerobot.utils.feature_utils.hw_to_dataset_features` to
        derive the LeRobotDataset feature spec.

        Empty dicts are acceptable when the adapter has no hardware bound
        (fake-devices mode); the recorder records an empty-frame dataset so
        the UI flow is still exercisable without a real robot.
        """


@dataclass(slots=True)
class _LiveSlot:
    status: RobotStatus = field(default_factory=RobotStatus)
    entry: RobotEntry | None = None


class InMemoryRobotManager:
    """Fallback :class:`RobotManagerProtocol` that never touches hardware.

    Used in tests and in ``fake_devices`` mode. It honors the lifecycle
    (connect/disconnect/status) so frontends can exercise the full UI
    without actual robots present.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._slots: dict[UUID, _LiveSlot] = {}

    async def connect(self, entry: RobotEntry, cameras: list[CameraEntry]) -> RobotStatus:
        async with self._lock:
            slot = self._slots.setdefault(entry.id, _LiveSlot())
            slot.entry = entry
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
                # Idempotent disconnect of an unknown robot — report offline.
                return RobotStatus(online=False)
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

    async def send_action(self, robot_id: UUID, action: dict[str, float]) -> None:
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None or not slot.status.online:
                return  # Silently drop — simulated hardware is offline.

    async def read_observation(self, robot_id: UUID) -> dict[str, Any]:
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None or not slot.status.online:
                return {}
            return {
                "robot_id": str(robot_id),
                "online": True,
                "timestamp": datetime.now(UTC).isoformat(),
                "joints": {},
            }

    async def get_features(self, robot_id: UUID) -> dict[str, dict]:
        async with self._lock:
            slot = self._slots.get(robot_id)
            if slot is None:
                return {"observation": {}, "action": {}}
            # Fake-devices mode has no real joints; return empty feature dicts
            # so the recorder can still exercise its end-to-end flow.
            return {"observation": {}, "action": {}}
