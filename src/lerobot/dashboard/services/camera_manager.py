"""Protocol and in-memory fallback for the live camera manager.

Mirrors :mod:`lerobot.dashboard.services.robot_manager`: we declare a
Protocol that the HTTP + streaming layers depend on, and ship a no-op
:class:`InMemoryCameraManager` fallback so the backend imports without
hardware SDKs (OpenCV, librealsense, ...) present.

Streaming consumers (``src/lerobot/dashboard/streaming/``) call
:meth:`CameraManagerProtocol.subscribe` to pull frames; the signature
intentionally mirrors :class:`lerobot.dashboard.streaming.source.FrameSource`
so a thin adapter is all that's needed to bridge the two.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

import numpy as np

from lerobot.dashboard.services.registry_models import CameraEntry, CameraStatus


@runtime_checkable
class CameraManagerProtocol(Protocol):
    """Live runtime for dashboard-declared cameras."""

    async def open(self, entry: CameraEntry) -> CameraStatus:
        """Acquire hardware handles for ``entry`` and mark it open."""

    async def close(self, camera_id: UUID) -> CameraStatus:
        """Release hardware handles for ``camera_id``."""

    async def is_open(self, camera_id: UUID) -> bool:
        """Return ``True`` if the camera is currently capturing."""

    async def get_status(self, camera_id: UUID) -> CameraStatus:
        """Return the most recent runtime status for ``camera_id``."""

    def subscribe(self, camera_id: UUID) -> AsyncIterator[np.ndarray]:
        """Yield HxWx3 ``uint8`` BGR frames until the subscriber is cancelled.

        Each subscriber gets an independent iterator; dropping the iterator
        (GC or explicit ``aclose``) decrements :attr:`CameraStatus.subscriber_count`.
        """


@dataclass(slots=True)
class _CameraSlot:
    status: CameraStatus = field(default_factory=CameraStatus)
    entry: CameraEntry | None = None
    subscribers: int = 0


class InMemoryCameraManager:
    """Fallback :class:`CameraManagerProtocol` that never touches hardware.

    :meth:`subscribe` yields zero-filled frames at the camera's declared
    fps — enough to drive WebRTC negotiation in tests and ``fake_devices``
    mode without pulling in OpenCV.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._slots: dict[UUID, _CameraSlot] = {}

    async def open(self, entry: CameraEntry) -> CameraStatus:
        async with self._lock:
            slot = self._slots.setdefault(entry.id, _CameraSlot())
            slot.entry = entry
            slot.status = CameraStatus(
                open=True,
                last_error=None,
                opened_at=datetime.now(UTC),
                subscriber_count=slot.subscribers,
            )
            return slot.status.model_copy()

    async def close(self, camera_id: UUID) -> CameraStatus:
        async with self._lock:
            slot = self._slots.get(camera_id)
            if slot is None:
                return CameraStatus()
            slot.status = CameraStatus(
                open=False,
                last_error=slot.status.last_error,
                opened_at=slot.status.opened_at,
                subscriber_count=slot.subscribers,
            )
            return slot.status.model_copy()

    async def is_open(self, camera_id: UUID) -> bool:
        async with self._lock:
            slot = self._slots.get(camera_id)
            return bool(slot and slot.status.open)

    async def get_status(self, camera_id: UUID) -> CameraStatus:
        async with self._lock:
            slot = self._slots.get(camera_id)
            if slot is None:
                return CameraStatus()
            return slot.status.model_copy(update={"subscriber_count": slot.subscribers})

    async def _increment(self, camera_id: UUID) -> None:
        async with self._lock:
            slot = self._slots.get(camera_id)
            if slot is not None:
                slot.subscribers += 1

    async def _decrement(self, camera_id: UUID) -> None:
        async with self._lock:
            slot = self._slots.get(camera_id)
            if slot is not None:
                slot.subscribers = max(0, slot.subscribers - 1)

    async def subscribe(self, camera_id: UUID) -> AsyncIterator[np.ndarray]:
        slot = self._slots.get(camera_id)
        if slot is None or slot.entry is None or not slot.status.open:
            return
        entry = slot.entry
        fps = max(int(entry.fps), 1)
        await self._increment(camera_id)
        try:
            while True:
                slot = self._slots.get(camera_id)
                if slot is None or not slot.status.open:
                    return
                yield np.zeros((entry.height, entry.width, 3), dtype=np.uint8)
                await asyncio.sleep(1.0 / fps)
        finally:
            await self._decrement(camera_id)
