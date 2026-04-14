"""Async video track fed by a :class:`FrameSource`.

Responsibilities extracted from the Task #9 spec:

* Convert BGR ``ndarray`` frames to PyAV ``VideoFrame`` objects with a
  strictly monotonic pts aligned to a 90 kHz clock (what RTP expects).
* Keep streaming even if the underlying camera stalls: on timeout or
  exception we yield the most recent known frame (or black if none) and
  schedule a reconnect attempt. This matches the "track goes black /
  reattempt every 1s" language in the task description.
* Let the session layer resize frames on the fly via :meth:`set_size`
  — :meth:`recv` re-reads the declared width/height on every frame so a
  quality PATCH takes effect without having to rebuild the track.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import time
from typing import Callable

import av
import numpy as np
from aiortc import VideoStreamTrack

from lerobot.dashboard.streaming.source import FrameSource, FrameSourceError, _LastFrameCache

logger = logging.getLogger(__name__)

_RTP_CLOCK_RATE = 90_000
_DEFAULT_FRAME_TIMEOUT_S = 1.0
_DEFAULT_STALE_FRAME_MAX_AGE_S = 2.0


class LeRobotCameraTrack(VideoStreamTrack):
    """WebRTC video track adapting a :class:`FrameSource` to aiortc."""

    kind = "video"

    def __init__(
        self,
        source: FrameSource,
        *,
        width: int | None = None,
        height: int | None = None,
        frame_timeout_s: float = _DEFAULT_FRAME_TIMEOUT_S,
        stale_frame_max_age_s: float = _DEFAULT_STALE_FRAME_MAX_AGE_S,
        on_recover: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._source = source
        self._width = width or source.width
        self._height = height or source.height
        self._frame_timeout_s = frame_timeout_s
        self._time_base = fractions.Fraction(1, _RTP_CLOCK_RATE)
        self._start_monotonic_s: float | None = None
        self._cache = _LastFrameCache()
        self._stale_max_age_s = stale_frame_max_age_s
        self._consecutive_failures = 0
        self._on_recover = on_recover

    def set_size(self, width: int, height: int) -> None:
        """Update the output resolution applied on the next :meth:`recv` call."""
        self._width = int(width)
        self._height = int(height)

    @property
    def size(self) -> tuple[int, int]:
        return self._width, self._height

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    async def recv(self) -> av.VideoFrame:
        ndarray = await self._read_with_fallback()
        ndarray = self._fit(ndarray)
        frame = av.VideoFrame.from_ndarray(ndarray, format="bgr24")
        frame.pts, frame.time_base = self._next_pts()
        return frame

    async def _read_with_fallback(self) -> np.ndarray:
        try:
            ndarray = await asyncio.wait_for(self._source.read(), timeout=self._frame_timeout_s)
            if self._consecutive_failures > 0 and self._on_recover is not None:
                try:
                    self._on_recover()
                except Exception:  # pragma: no cover - recovery hook is best-effort
                    logger.exception("on_recover hook raised")
            self._consecutive_failures = 0
            self._cache.set(ndarray)
            return ndarray
        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            logger.debug("frame source timeout (failures=%d)", self._consecutive_failures)
        except FrameSourceError as exc:
            self._consecutive_failures += 1
            logger.warning("frame source error: %s (failures=%d)", exc, self._consecutive_failures)
        except Exception as exc:  # pragma: no cover - belt-and-braces
            self._consecutive_failures += 1
            logger.exception("unexpected frame source failure: %s", exc)

        cached = self._cache.get(self._stale_max_age_s)
        if cached is not None:
            return cached
        # Camera fully gone and no recent frame — emit black so negotiation
        # stays healthy while the higher layer attempts reconnect.
        return np.zeros((self._height, self._width, 3), dtype=np.uint8)

    def _fit(self, ndarray: np.ndarray) -> np.ndarray:
        """Resize ``ndarray`` to the declared width/height if needed.

        We use ``av.VideoFrame`` for the scale rather than pulling cv2 in
        as a new dependency; that keeps the streaming extra slim and
        works on all platforms aiortc supports.
        """
        h, w = ndarray.shape[:2]
        if w == self._width and h == self._height:
            return ndarray
        src = av.VideoFrame.from_ndarray(ndarray, format="bgr24")
        resized = src.reformat(width=self._width, height=self._height, format="bgr24")
        return resized.to_ndarray(format="bgr24")

    def _next_pts(self) -> tuple[int, fractions.Fraction]:
        now_s = time.monotonic()
        if self._start_monotonic_s is None:
            self._start_monotonic_s = now_s
        elapsed = now_s - self._start_monotonic_s
        return int(elapsed / self._time_base), self._time_base
