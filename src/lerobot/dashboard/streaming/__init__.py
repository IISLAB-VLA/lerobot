"""WebRTC streaming stack for the LeRobot dashboard (task #9).

The package implements the server half of a browser-to-robot live video
channel:

* :mod:`.source` — the :class:`FrameSource` / :class:`FrameSourceProvider`
  protocols that describe how cameras expose BGR frames.
* :mod:`.track` — :class:`LeRobotCameraTrack`, an ``aiortc`` video track
  that adapts a :class:`FrameSource` into RTP packets.
* :mod:`.codecs` — codec preference helpers (VP9 > H264 > VP8).
* :mod:`.session` — per-peer :class:`StreamSession` tracking and the
  :class:`SignalingManager` that owns the live :class:`RTCPeerConnection`
  pool.
* :mod:`.router` — FastAPI routes mounted under ``/api/streams``.
"""

from lerobot.dashboard.streaming.codecs import (
    CODEC_PREFERENCE,
    apply_codec_preference,
)
from lerobot.dashboard.streaming.router import build_streams_router
from lerobot.dashboard.streaming.session import (
    QualitySettings,
    SignalingManager,
    StreamSession,
)
from lerobot.dashboard.streaming.source import (
    FrameSource,
    FrameSourceError,
    FrameSourceProvider,
    StubFrameSource,
    StubFrameSourceProvider,
)
from lerobot.dashboard.streaming.track import LeRobotCameraTrack

__all__ = [
    "CODEC_PREFERENCE",
    "FrameSource",
    "FrameSourceError",
    "FrameSourceProvider",
    "LeRobotCameraTrack",
    "QualitySettings",
    "SignalingManager",
    "StreamSession",
    "StubFrameSource",
    "StubFrameSourceProvider",
    "apply_codec_preference",
    "build_streams_router",
]
