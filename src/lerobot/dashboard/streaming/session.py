"""Per-peer WebRTC session bookkeeping.

One :class:`StreamSession` owns an :class:`aiortc.RTCPeerConnection`, the
:class:`LeRobotCameraTrack` feeding it, and the :class:`FrameSource`
behind that track. Sessions are indexed by an opaque ``session_id`` so
the router layer can address them for ICE trickling, quality updates,
stats, and tear-down.

The :class:`SignalingManager` is the collection-level façade that the
router holds: ``create_session`` → build PC + track, apply codec
preferences, run the offer/answer dance; ``add_ice_candidate`` forwards
trickled candidates; ``update_quality`` reconfigures the encoder in
place; ``stats`` returns a flattened snapshot; ``close_session`` /
``close_all`` release resources.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from lerobot.dashboard.streaming.codecs import apply_codec_preference
from lerobot.dashboard.streaming.source import FrameSource, FrameSourceProvider
from lerobot.dashboard.streaming.track import LeRobotCameraTrack

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QualitySettings:
    """Encoder knobs exposed by ``PATCH /api/streams/{sid}/quality``.

    ``bitrate_kbps`` maps to the sender's ``RTCRtpEncodingParameters``
    ``maxBitrate`` (bits/s). ``width``/``height`` resize the outgoing
    frames by mutating the track's declared size — aiortc renegotiates
    the frame size frame-by-frame, no re-SDP needed.
    """

    width: int | None = None
    height: int | None = None
    fps: int | None = None
    bitrate_kbps: int | None = None

    def has_size(self) -> bool:
        return self.width is not None and self.height is not None


@dataclass
class StreamSession:
    """Live state for one browser peer.

    ``session_id`` is an opaque UUID4 string. ``robot_id``/``camera_id``
    echo the request that created the session so operators can tie a
    session back to its hardware from logs. ``applied_codecs`` records
    what :func:`apply_codec_preference` actually filtered to — useful
    telemetry for the UI overlay.
    """

    session_id: str
    robot_id: str
    camera_id: str
    pc: RTCPeerConnection
    track: LeRobotCameraTrack
    source: FrameSource
    applied_codecs: list[str] = field(default_factory=list)
    quality: QualitySettings = field(default_factory=QualitySettings)
    # Target encoder bitrate in bits/s — applied lazily because aiortc
    # instantiates the encoder only on first frame. Re-applied on every
    # ``request_keyframe`` (including auto-fires on camera recovery).
    desired_bitrate_bps: int | None = None
    # Count of successful keyframe requests — exposed in stats for tests.
    keyframes_requested: int = 0


class SignalingManager:
    """Owns the set of live :class:`StreamSession` objects."""

    def __init__(self, provider: FrameSourceProvider) -> None:
        self._provider = provider
        self._sessions: dict[str, StreamSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        *,
        robot_id: str,
        camera_id: str,
        offer_sdp: str,
        offer_type: str,
    ) -> tuple[StreamSession, RTCSessionDescription]:
        """Build a session from a browser offer and return (session, answer)."""
        source = await self._provider.open(robot_id, camera_id)
        pc = RTCPeerConnection()

        session_id = uuid.uuid4().hex

        def _on_recover() -> None:
            # Fired when the track transitions from failing back to live.
            # We request a keyframe so the browser decoder can resync without
            # waiting for the next natural keyframe interval.
            self._request_keyframes_sync(session_id)

        track = LeRobotCameraTrack(source, on_recover=_on_recover)
        sender = pc.addTrack(track)
        applied = apply_codec_preference(sender)

        session = StreamSession(
            session_id=session_id,
            robot_id=robot_id,
            camera_id=camera_id,
            pc=pc,
            track=track,
            source=source,
            applied_codecs=applied,
        )

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            logger.info("session %s: connection state %s", session.session_id, pc.connectionState)
            if pc.connectionState in {"failed", "closed"}:
                await self.close_session(session.session_id)
            elif pc.connectionState == "connected":
                # Fresh connect / ICE restart — ensure the decoder gets a
                # keyframe right away and any pending bitrate target lands.
                self._request_keyframes_sync(session.session_id)
                if session.desired_bitrate_bps is not None:
                    _apply_bitrate(pc, session.desired_bitrate_bps)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        answer_desc = RTCSessionDescription(
            sdp=pc.localDescription.sdp, type=pc.localDescription.type
        )

        async with self._lock:
            self._sessions[session.session_id] = session
        logger.info(
            "session %s created (robot=%s camera=%s codecs=%s)",
            session.session_id,
            robot_id,
            camera_id,
            applied,
        )
        return session, answer_desc

    async def add_ice_candidate(self, session_id: str, payload: dict[str, Any]) -> None:
        session = self._require(session_id)
        candidate = _parse_ice_candidate(payload)
        if candidate is None:
            # null candidate = end-of-candidates signal; aiortc handles this implicitly.
            return
        await session.pc.addIceCandidate(candidate)

    async def update_quality(self, session_id: str, quality: QualitySettings) -> QualitySettings:
        """Apply new encoder settings; returns the merged effective settings."""
        session = self._require(session_id)
        merged = QualitySettings(
            width=quality.width if quality.width is not None else session.quality.width,
            height=quality.height if quality.height is not None else session.quality.height,
            fps=quality.fps if quality.fps is not None else session.quality.fps,
            bitrate_kbps=(
                quality.bitrate_kbps
                if quality.bitrate_kbps is not None
                else session.quality.bitrate_kbps
            ),
        )
        if merged.has_size():
            session.track.set_size(merged.width, merged.height)  # type: ignore[arg-type]

        if merged.bitrate_kbps is not None:
            target_bps = max(int(merged.bitrate_kbps) * 1000, 10_000)
            session.desired_bitrate_bps = target_bps
            _apply_bitrate(session.pc, target_bps)

        session.quality = merged
        logger.info("session %s quality updated: %s", session_id, merged)
        return merged

    async def request_keyframe(self, session_id: str) -> int:
        """Force every video sender on the session to emit a keyframe.

        Returns the number of senders that were successfully signalled.
        """
        session = self._require(session_id)
        return self._request_keyframes_sync(session_id, session=session)

    def _request_keyframes_sync(
        self, session_id: str, *, session: StreamSession | None = None
    ) -> int:
        """Non-async variant usable from synchronous aiortc callbacks."""
        session = session or self._sessions.get(session_id)
        if session is None:
            return 0
        signalled = 0
        for sender in session.pc.getSenders():
            if sender.track is None or sender.track.kind != "video":
                continue
            try:
                sender._send_keyframe()
                signalled += 1
            except Exception:  # pragma: no cover - best-effort keyframe
                logger.debug("sender._send_keyframe unavailable; skipping")
        if signalled and session.desired_bitrate_bps is not None:
            # Keyframe is a good moment to re-assert the bitrate target —
            # the encoder has definitely been instantiated by now.
            _apply_bitrate(session.pc, session.desired_bitrate_bps)
        session.keyframes_requested += signalled
        return signalled

    async def stats(self, session_id: str) -> list[dict[str, Any]]:
        session = self._require(session_id)
        report = await session.pc.getStats()
        return [_flatten_stat(entry) for entry in report.values()]

    async def stats_many(
        self, session_ids: list[str] | None = None
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """Gather stats for several sessions in one pass.

        ``session_ids=None`` returns every live session. Otherwise only the
        listed ids are queried. Sessions that have just been closed (race
        against a concurrent ``close_session``) are silently omitted so the
        UI can treat "missing sid" as "unsubscribe this tile" without
        needing an explicit ``state`` field.
        """
        async with self._lock:
            candidates = (
                list(self._sessions.values())
                if session_ids is None
                else [self._sessions[sid] for sid in session_ids if sid in self._sessions]
            )
        # getStats() coroutines run without the manager lock — holding it
        # across the aiortc call would serialise stats across sessions and
        # defeat the batch point.
        results: list[tuple[str, list[dict[str, Any]]]] = []
        for session in candidates:
            try:
                report = await session.pc.getStats()
            except Exception:  # pragma: no cover - pc may have closed mid-call
                logger.debug("stats() skipped for %s (pc unavailable)", session.session_id)
                continue
            results.append(
                (session.session_id, [_flatten_stat(entry) for entry in report.values()])
            )
        return results

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            await session.pc.close()
        finally:
            try:
                await session.source.close()
            except Exception:  # pragma: no cover - source close is best-effort
                logger.exception("error closing source for session %s", session_id)
        logger.info("session %s closed", session_id)

    async def close_all(self) -> None:
        async with self._lock:
            ids = list(self._sessions)
        for sid in ids:
            await self.close_session(sid)

    def get(self, session_id: str) -> StreamSession | None:
        return self._sessions.get(session_id)

    def _require(self, session_id: str) -> StreamSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session


_CANDIDATE_PREFIX_RE = re.compile(r"^candidate:")


def _parse_ice_candidate(payload: dict[str, Any]) -> RTCIceCandidate | None:
    """Build an :class:`RTCIceCandidate` from the browser's trickle payload.

    Browsers send ``{candidate, sdpMid, sdpMLineIndex}`` where ``candidate``
    is the SDP a=candidate line *without* the ``a=`` prefix. An empty
    candidate string signals end-of-candidates; we return ``None`` so the
    caller can no-op.
    """
    raw = (payload.get("candidate") or "").strip()
    if not raw:
        return None
    # aiortc's parser wants the attribute value without the leading "candidate:".
    stripped = _CANDIDATE_PREFIX_RE.sub("", raw)
    candidate = candidate_from_sdp(stripped)
    candidate.sdpMid = payload.get("sdpMid")
    sdp_mline_index = payload.get("sdpMLineIndex")
    if sdp_mline_index is not None:
        candidate.sdpMLineIndex = int(sdp_mline_index)
    return candidate


def _apply_bitrate(pc: RTCPeerConnection, bitrate_bps: int) -> int:
    """Set the target bitrate on every video sender's live encoder.

    aiortc 1.14 does not expose ``RTCRtpSender.setParameters`` — the only
    lever on the bitrate is the encoder's ``target_bitrate`` attribute
    (``H264Encoder`` / ``Vp8Encoder``). The encoder is created lazily
    inside aiortc when the first frame flows, so this helper silently
    no-ops if no encoder is live yet. Returns the count of encoders that
    were updated so callers can schedule a retry.
    """
    updated = 0
    for sender in pc.getSenders():
        if sender.track is None or sender.track.kind != "video":
            continue
        encoder = _sender_encoder(sender)
        if encoder is None or not hasattr(encoder, "target_bitrate"):
            continue
        encoder.target_bitrate = int(bitrate_bps)
        updated += 1
    return updated


def _sender_encoder(sender: Any) -> Any | None:
    """Reach into aiortc's name-mangled ``__encoder`` slot on the sender."""
    return getattr(sender, "_RTCRtpSender__encoder", None)


def _flatten_stat(entry: Any) -> dict[str, Any]:
    """Reduce an aiortc stats entry to a JSON-friendly dict."""
    out: dict[str, Any] = {}
    for key in dir(entry):
        if key.startswith("_"):
            continue
        try:
            value = getattr(entry, key)
        except Exception:
            continue
        if callable(value):
            continue
        out[key] = _jsonify(value)
    return out


def _jsonify(value: Any) -> Any:
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    return str(value)
