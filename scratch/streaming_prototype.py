"""aiortc streaming prototype for lerobot dashboard.

Validates the building blocks needed for Task #9:
- numpy BGR frame -> PyAV VideoFrame conversion
- VideoStreamTrack subclass backed by a pluggable async frame source
- RTCPeerConnection offer/answer roundtrip with codec preference VP9>H264>VP8
- getStats() telemetry snapshot

Run as a script for a manual end-to-end handshake:
    uv run python scratch/streaming_prototype.py

Automated checks live in tests/dashboard/test_streaming_prototype.py.
"""

from __future__ import annotations

import asyncio
import fractions
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import av
import numpy as np
from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription, VideoStreamTrack
from aiortc.codecs import h264  # noqa: F401 - ensures codec registration
from aiortc.rtcrtpsender import RTCRtpSender as _Sender  # re-expose for codec filtering

CODEC_PREFERENCE = ("video/VP9", "video/H264", "video/VP8")
"""Tuple scanned in order when narrowing a sender's codecs.

Note on aiortc 1.14.0: VP9 is not shipped (only VP8, H264 x2 profiles, RTX).
We keep VP9 first so a future aiortc release automatically picks it up; the
effective order today is H264 > VP8. Glass-to-glass latency budget of 200 ms
is reachable with H264 baseline (profile-level-id=42001f/42e01f).
"""


FrameSource = Callable[[], Awaitable[np.ndarray]]
"""Awaitable returning the next BGR uint8 HxWx3 frame."""


@dataclass
class SyntheticFrameSource:
    """Generates an animated BGR frame at a fixed fps.

    Stands in for camera_manager.subscribe() until Task #6 lands; the real
    adapter should expose the same awaitable contract.
    """

    width: int = 640
    height: int = 480
    fps: int = 30
    _frame_idx: int = 0

    async def __call__(self) -> np.ndarray:
        await asyncio.sleep(1.0 / self.fps)
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        t = self._frame_idx
        frame[:, :, 0] = (t * 3) % 256  # B
        frame[:, :, 1] = (t * 5) % 256  # G
        frame[:, :, 2] = (t * 7) % 256  # R
        self._frame_idx += 1
        return frame


class LeRobotCameraTrack(VideoStreamTrack):
    """WebRTC video track fed by an async BGR frame source.

    Responsibilities this prototype pins down:
    - monotonic pts/time_base assignment (aiortc requires strictly increasing pts)
    - BGR ndarray -> VideoFrame(format='bgr24') conversion via PyAV
    - graceful fallback to a black frame when the source raises or times out,
      matching the Task #9 reconnect spec
    """

    kind = "video"

    def __init__(
        self,
        source: FrameSource,
        *,
        width: int,
        height: int,
        clock_rate: int = 90_000,
        frame_timeout_s: float = 1.0,
    ) -> None:
        super().__init__()
        self._source = source
        self._width = width
        self._height = height
        self._time_base = fractions.Fraction(1, clock_rate)
        self._frame_timeout_s = frame_timeout_s
        self._start_monotonic_s: float | None = None

    async def recv(self) -> av.VideoFrame:
        try:
            ndarray = await asyncio.wait_for(self._source(), timeout=self._frame_timeout_s)
        except (asyncio.TimeoutError, Exception):
            ndarray = np.zeros((self._height, self._width, 3), dtype=np.uint8)

        frame = av.VideoFrame.from_ndarray(ndarray, format="bgr24")
        now_s = time.monotonic()
        if self._start_monotonic_s is None:
            self._start_monotonic_s = now_s
        elapsed_s = now_s - self._start_monotonic_s
        frame.pts = int(elapsed_s / self._time_base)
        frame.time_base = self._time_base
        return frame


def prefer_codecs(sender: RTCRtpSender, preference: tuple[str, ...] = CODEC_PREFERENCE) -> list[str]:
    """Narrow a sender's codecs to those whose mimeType is in ``preference``.

    Returns the mimeTypes that were kept, in priority order. When no preferred
    codec is available we leave the sender's codec list untouched and return an
    empty list so the caller can fall back to the browser's default.
    """
    capabilities = _Sender.getCapabilities("video")
    if capabilities is None:
        return []
    by_mime: dict[str, list] = {}
    for codec in capabilities.codecs:
        by_mime.setdefault(codec.mimeType, []).append(codec)

    ordered = [c for mime in preference for c in by_mime.get(mime, [])]
    if not ordered:
        return []

    # Many aiortc versions expose setCodecPreferences on the transceiver, not the sender.
    # We hunt for the transceiver that owns this sender and apply there.
    transceiver = getattr(sender, "_transceiver", None) or getattr(sender, "transceiver", None)
    if transceiver is not None and hasattr(transceiver, "setCodecPreferences"):
        transceiver.setCodecPreferences(ordered)
    return [c.mimeType for c in ordered]


async def handshake(
    offerer_track: VideoStreamTrack,
) -> tuple[RTCPeerConnection, RTCPeerConnection, list[str]]:
    """Drive a local offer/answer handshake between two RTCPeerConnections.

    Returns the (offerer, answerer, preferred_codecs_applied) triple. The
    caller is responsible for closing both peer connections.
    """
    offerer = RTCPeerConnection()
    answerer = RTCPeerConnection()

    sender = offerer.addTrack(offerer_track)
    applied = prefer_codecs(sender)

    @answerer.on("track")
    def _on_track(track):  # pragma: no cover - receiver callback
        async def drain():
            try:
                await track.recv()
            except Exception:
                pass

        asyncio.ensure_future(drain())

    offer = await offerer.createOffer()
    await offerer.setLocalDescription(offer)
    await answerer.setRemoteDescription(
        RTCSessionDescription(sdp=offerer.localDescription.sdp, type=offerer.localDescription.type)
    )
    answer = await answerer.createAnswer()
    await answerer.setLocalDescription(answer)
    await offerer.setRemoteDescription(
        RTCSessionDescription(sdp=answerer.localDescription.sdp, type=answerer.localDescription.type)
    )
    return offerer, answerer, applied


async def iter_stats(pc: RTCPeerConnection) -> AsyncIterator[dict]:
    """Yield a flattened snapshot of ``pc.getStats()`` suitable for the UI overlay."""
    report = await pc.getStats()
    for entry in report.values():
        yield {k: getattr(entry, k) for k in entry.__dict__ if not k.startswith("_")}


async def _main() -> None:
    source = SyntheticFrameSource()
    track = LeRobotCameraTrack(source, width=source.width, height=source.height)
    offerer, answerer, applied = await handshake(track)
    try:
        print(f"Applied codec preference: {applied}")
        await asyncio.sleep(1.0)
        print("Offerer stats sample:")
        async for stat in iter_stats(offerer):
            print(" ", stat.get("type"), stat.get("id"))
    finally:
        await offerer.close()
        await answerer.close()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
