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
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


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
