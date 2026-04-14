"""Protocol and in-memory fallback for the live teleop manager.

Mirrors :mod:`camera_manager`. The HTTP + WebSocket layers depend on
this Protocol so the dashboard can load without gamepad/keyboard
hardware wired up. Task #11 layers a WebSocket endpoint on top that
forwards :class:`TeleopEvent` payloads through :meth:`handle_input`.

``TeleopEvent`` and the concrete event dataclasses live in
:mod:`lerobot.dashboard.teleop.protocol` — the WS envelope module owns
the wire format, this module just forwards already-parsed events.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from lerobot.dashboard.services.registry_models import TeleopEntry, TeleopStatus
from lerobot.dashboard.teleop.protocol import TeleopEvent


@runtime_checkable
class TeleopManagerProtocol(Protocol):
    """Live runtime for dashboard-declared teleop devices."""

    async def attach(self, entry: TeleopEntry, robot_id: UUID) -> TeleopStatus:
        """Bind ``entry`` to ``robot_id`` and mark it attached."""

    async def detach(self, teleop_id: UUID) -> TeleopStatus:
        """Unbind the teleop from its robot."""

    async def is_attached(self, teleop_id: UUID) -> bool:
        """Return ``True`` if currently attached to a robot."""

    async def get_status(self, teleop_id: UUID) -> TeleopStatus:
        """Return the most recent runtime status for ``teleop_id``."""

    async def handle_input(self, teleop_id: UUID, event: TeleopEvent) -> None:
        """Forward a single user-input event.

        Implementations typically translate ``event`` into a robot action
        and call :meth:`RobotManagerProtocol.send_action`. No-op when the
        teleop is not attached.
        """


@dataclass(slots=True)
class _TeleopSlot:
    status: TeleopStatus = field(default_factory=TeleopStatus)
    entry: TeleopEntry | None = None
    recent_events: deque[TeleopEvent] = field(default_factory=lambda: deque(maxlen=16))


class InMemoryTeleopManager:
    """Fallback :class:`TeleopManagerProtocol` with no hardware coupling.

    :meth:`handle_input` records the last 16 events per teleop so tests
    can inspect the forwarded payloads without spinning up a real robot
    adapter.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._slots: dict[UUID, _TeleopSlot] = {}

    async def attach(self, entry: TeleopEntry, robot_id: UUID) -> TeleopStatus:
        async with self._lock:
            slot = self._slots.setdefault(entry.id, _TeleopSlot())
            slot.entry = entry
            slot.status = TeleopStatus(
                attached=True,
                bound_robot_id=robot_id,
                last_error=None,
                attached_at=datetime.now(UTC),
            )
            return slot.status.model_copy()

    async def detach(self, teleop_id: UUID) -> TeleopStatus:
        async with self._lock:
            slot = self._slots.get(teleop_id)
            if slot is None:
                return TeleopStatus()
            slot.status = TeleopStatus(
                attached=False,
                bound_robot_id=None,
                last_error=slot.status.last_error,
                attached_at=slot.status.attached_at,
            )
            return slot.status.model_copy()

    async def is_attached(self, teleop_id: UUID) -> bool:
        async with self._lock:
            slot = self._slots.get(teleop_id)
            return bool(slot and slot.status.attached)

    async def get_status(self, teleop_id: UUID) -> TeleopStatus:
        async with self._lock:
            slot = self._slots.get(teleop_id)
            if slot is None:
                return TeleopStatus()
            return slot.status.model_copy()

    async def handle_input(self, teleop_id: UUID, event: TeleopEvent) -> None:
        async with self._lock:
            slot = self._slots.get(teleop_id)
            if slot is None or not slot.status.attached:
                return
            slot.recent_events.append(event)

    async def recent_events(self, teleop_id: UUID) -> list[TeleopEvent]:
        """Test hook — not part of the Protocol, InMemory-only."""
        async with self._lock:
            slot = self._slots.get(teleop_id)
            if slot is None:
                return []
            return list(slot.recent_events)
