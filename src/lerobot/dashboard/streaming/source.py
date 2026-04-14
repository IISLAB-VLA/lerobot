"""Pluggable camera frame sources for the streaming stack.

Until Task #6 (Robot/Camera adapter layer) lands, WebRTC tracks are fed by
a :class:`StubFrameSource` that synthesises animated frames. The real
adapter from camera_manager will implement :class:`FrameSource` and can be
swapped in without touching :mod:`.track` or :mod:`.session`.

Contract:

* :class:`FrameSource` is an async iterator of HxWx3 ``uint8`` BGR frames.
  Sources throttle themselves to their declared fps — callers simply
  ``await source.read()``.
* :class:`FrameSourceProvider` resolves ``(robot_id, camera_id)`` to a
  :class:`FrameSource`. :class:`StubFrameSourceProvider` returns a shared
  :class:`StubFrameSource` regardless of the identifiers — useful for
  tests and for running the dashboard before hardware is wired up
  (``LEROBOT_DASHBOARD_FAKE_DEVICES=1``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

import numpy as np

if TYPE_CHECKING:
    from lerobot.dashboard.services.camera_manager import CameraManagerProtocol
    from lerobot.dashboard.services.registry import Registry

logger = logging.getLogger(__name__)


class FrameSourceError(RuntimeError):
    """Raised by :meth:`FrameSource.read` when the underlying camera is gone."""


@runtime_checkable
class FrameSource(Protocol):
    """Async source of BGR frames consumed by :class:`LeRobotCameraTrack`."""

    width: int
    height: int
    fps: int

    async def read(self) -> np.ndarray:
        """Return the next HxWx3 ``uint8`` BGR frame, blocking until available."""

    async def close(self) -> None:
        """Release any resources (subscription handles, threads, etc.)."""


@runtime_checkable
class FrameSourceProvider(Protocol):
    """Resolves ``(robot_id, camera_id)`` to a live :class:`FrameSource`."""

    async def open(self, robot_id: str, camera_id: str) -> FrameSource:
        """Return a fresh :class:`FrameSource` for the requested camera.

        Raises :class:`FrameSourceError` if the camera is unknown or offline.
        """


@dataclass
class StubFrameSource:
    """Synthesises an animated BGR pattern at a fixed fps.

    The output is a horizontal gradient that shifts over time so a human
    observer can eyeball frame freshness. Intended for unit tests and the
    ``LEROBOT_DASHBOARD_FAKE_DEVICES`` mode; replace with the camera_manager
    adapter once Task #6 lands.
    """

    width: int = 640
    height: int = 480
    fps: int = 30
    _frame_idx: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)

    async def read(self) -> np.ndarray:
        if self._closed:
            raise FrameSourceError("stub frame source is closed")
        # Throttle to declared fps without drifting on slow consumers.
        await asyncio.sleep(1.0 / max(self.fps, 1))
        t = self._frame_idx
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:, :, 0] = (t * 3) % 256
        frame[:, :, 1] = (t * 5) % 256
        frame[:, :, 2] = (t * 7) % 256
        self._frame_idx += 1
        return frame

    async def close(self) -> None:
        self._closed = True


@dataclass
class StubFrameSourceProvider:
    """Returns a fresh :class:`StubFrameSource` per ``open()`` call.

    The provider is stateless — callers are responsible for closing the
    returned sources. Size/fps defaults match a typical webcam and can be
    tuned through the constructor for tests that want tiny frames.
    """

    width: int = 640
    height: int = 480
    fps: int = 30

    async def open(self, robot_id: str, camera_id: str) -> FrameSource:
        return StubFrameSource(width=self.width, height=self.height, fps=self.fps)


@dataclass
class CameraManagerFrameSource:
    """Adapter from :class:`CameraManagerProtocol.subscribe` to :class:`FrameSource`.

    The manager already owns per-subscriber backpressure (bounded queue,
    drop-oldest) so this wrapper is thin: on each :meth:`read` we pull
    the next frame from the subscription iterator. A graceful
    ``StopAsyncIteration`` is converted to :class:`FrameSourceError` so
    the track layer treats camera exhaustion the same as a hard
    disconnect and falls back to the stale-frame cache.
    """

    manager: "CameraManagerProtocol"
    camera_id: UUID
    width: int
    height: int
    fps: int
    _aiter: AsyncIterator[np.ndarray] | None = field(default=None, init=False, repr=False)

    async def read(self) -> np.ndarray:
        if self._aiter is None:
            # ``subscribe`` may be either a plain ``async def`` that returns an
            # async generator (aiortc's usual style) or an already-constructed
            # iterator; normalise both to ``__aiter__``.
            stream = self.manager.subscribe(self.camera_id)
            if asyncio.iscoroutine(stream):
                stream = await stream
            self._aiter = stream.__aiter__()
        try:
            return await self._aiter.__anext__()
        except StopAsyncIteration as exc:
            raise FrameSourceError(
                f"camera stream ended for {self.camera_id}"
            ) from exc

    async def close(self) -> None:
        aiter = self._aiter
        self._aiter = None
        if aiter is None:
            return
        aclose = getattr(aiter, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:  # pragma: no cover - best-effort close
            logger.exception("error closing camera subscription for %s", self.camera_id)


@dataclass
class RegistryFrameSourceProvider:
    """Look up ``(robot_id, camera_id)`` in the registry and bind a live source.

    Resolves ``camera_id`` through :class:`Registry`, ensures the
    :class:`CameraManagerProtocol` has it open (idempotent), then hands
    back a :class:`CameraManagerFrameSource` wrapping its subscription.
    Meant to be swapped in for :class:`StubFrameSourceProvider` when
    ``LEROBOT_DASHBOARD_FAKE_DEVICES`` is off.
    """

    manager: "CameraManagerProtocol"
    registry: "Registry"

    async def open(self, robot_id: str, camera_id: str) -> FrameSource:
        try:
            cid = UUID(camera_id)
        except ValueError as exc:
            raise FrameSourceError(f"invalid camera_id {camera_id!r}") from exc
        try:
            entry = await self.registry.get_camera(cid)
        except Exception as exc:
            # Registry raises its own typed error; re-cast so the track /
            # session layer sees the single FrameSourceError contract.
            raise FrameSourceError(f"camera {camera_id} not in registry") from exc
        # Open is idempotent on both the in-memory fallback and the real
        # adapter — calling it from here keeps the streaming layer usable
        # even if no REST ``connect`` fired first (tests, fake_devices).
        await self.manager.open(entry)
        return CameraManagerFrameSource(
            manager=self.manager,
            camera_id=cid,
            width=int(entry.width),
            height=int(entry.height),
            fps=int(entry.fps),
        )


class _LastFrameCache:
    """Tiny helper that remembers the most recent frame per source.

    Used by the reconnect path so we can show the last good frame while the
    underlying camera is rediscovering itself, instead of cutting to black.
    """

    __slots__ = ("frame", "ts")

    def __init__(self) -> None:
        self.frame: np.ndarray | None = None
        self.ts: float = 0.0

    def set(self, frame: np.ndarray) -> None:
        self.frame = frame
        self.ts = time.monotonic()

    def get(self, max_age_s: float) -> np.ndarray | None:
        if self.frame is None:
            return None
        if time.monotonic() - self.ts > max_age_s:
            return None
        return self.frame
