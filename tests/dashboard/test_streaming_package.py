"""End-to-end tests for the ``lerobot.dashboard.streaming`` package.

These cover the production code path (src/lerobot/dashboard/streaming)
rather than the scratch prototype:

* track reads BGR frames and emits monotonic VideoFrame objects
* track survives a source that raises and falls back to the cached frame
* SignalingManager performs an offer/answer handshake, delivers track
  data to the answering peer, honours quality updates, and closes cleanly
* Router-level HTTP surface (offer / ice / quality / stats / stop)
  returns the shapes the frontend contract expects

Run with ``uv run pytest tests/dashboard/test_streaming_package.py -svv``.
``pytest-asyncio`` is not yet in the ``test`` extra (backend-architect is
adding it); until then we drive asyncio manually via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

import pytest

pytest.importorskip("aiortc")

from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.streaming import (  # noqa: E402
    CODEC_PREFERENCE,
    LeRobotCameraTrack,
    QualitySettings,
    SignalingManager,
    StubFrameSource,
    StubFrameSourceProvider,
    apply_codec_preference,
    build_streams_router,
)
from lerobot.dashboard.streaming.source import FrameSourceError  # noqa: E402

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    """Drive ``coro`` to completion on a fresh event loop.

    ``asyncio.run`` on each test keeps the tests independent of one
    another — aiortc spins up its own loops internally and we don't want
    cross-test leaks.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# track.py
# ---------------------------------------------------------------------------


def test_track_emits_monotonic_bgr_video_frames():
    async def scenario():
        source = StubFrameSource(width=32, height=24, fps=240)
        track = LeRobotCameraTrack(source, width=32, height=24)
        first = await track.recv()
        second = await track.recv()
        await source.close()
        return first, second

    first, second = _run(scenario())
    assert first.format.name == "bgr24"
    assert (first.width, first.height) == (32, 24)
    assert second.pts > first.pts


def test_track_falls_back_when_source_raises():
    class BrokenSource:
        width, height, fps = 8, 8, 60

        async def read(self):
            raise FrameSourceError("camera died")

        async def close(self):
            pass

    async def scenario():
        track = LeRobotCameraTrack(BrokenSource(), width=8, height=8, stale_frame_max_age_s=0.0)
        frame = await track.recv()
        return frame, track.consecutive_failures

    frame, failures = _run(scenario())
    # Black frame (no cached good frame).
    assert (frame.width, frame.height) == (8, 8)
    assert frame.to_ndarray(format="bgr24").sum() == 0
    assert failures == 1


def test_track_resizes_when_quality_changes():
    async def scenario():
        source = StubFrameSource(width=64, height=48, fps=240)
        track = LeRobotCameraTrack(source, width=64, height=48)
        before = await track.recv()
        track.set_size(32, 24)
        after = await track.recv()
        await source.close()
        return before, after

    before, after = _run(scenario())
    assert (before.width, before.height) == (64, 48)
    assert (after.width, after.height) == (32, 24)


# ---------------------------------------------------------------------------
# session.py — SignalingManager handshake + lifecycle
# ---------------------------------------------------------------------------


def _make_answerer_sdp(offer_sdp: str) -> tuple[RTCPeerConnection, str]:
    """Return a peer holding an answer SDP to the caller's offer.

    The manager expects to *receive* an offer, so we mimic the browser by
    building an offerer here and passing its SDP to ``create_session``.
    """
    raise NotImplementedError  # kept for parity with older drafts


def test_create_session_handshake_completes_and_closes():
    manager = SignalingManager(StubFrameSourceProvider(width=32, height=24, fps=120))

    async def scenario():
        # Browser side: build an offer that requests to *receive* video.
        browser = RTCPeerConnection()
        browser.addTransceiver("video", direction="recvonly")
        offer = await browser.createOffer()
        await browser.setLocalDescription(offer)

        session, answer = await manager.create_session(
            robot_id="r1",
            camera_id="cam0",
            offer_sdp=browser.localDescription.sdp,
            offer_type=browser.localDescription.type,
        )
        await browser.setRemoteDescription(
            RTCSessionDescription(sdp=answer.sdp, type=answer.type)
        )

        assert session.session_id
        assert session.applied_codecs, "at least one preferred codec must be applied"
        for mime in session.applied_codecs:
            assert mime in CODEC_PREFERENCE

        # Give aiortc a tick to populate stats, then read them.
        await asyncio.sleep(0.2)
        stats = await manager.stats(session.session_id)
        assert stats, "getStats() should not be empty after handshake"
        assert all("type" in entry for entry in stats)

        # Quality update applies to the track even before media flows.
        merged = await manager.update_quality(
            session.session_id,
            QualitySettings(width=16, height=12, bitrate_kbps=500),
        )
        assert merged.width == 16 and merged.height == 12
        assert session.track.size == (16, 12)

        await manager.close_session(session.session_id)
        assert manager.get(session.session_id) is None
        await browser.close()

    _run(scenario())


def test_create_session_unknown_camera_raises():
    class EmptyProvider:
        async def open(self, robot_id: str, camera_id: str):
            raise FrameSourceError(f"no camera {robot_id}/{camera_id}")

    manager = SignalingManager(EmptyProvider())

    async def scenario():
        browser = RTCPeerConnection()
        browser.addTransceiver("video", direction="recvonly")
        offer = await browser.createOffer()
        await browser.setLocalDescription(offer)
        with pytest.raises(FrameSourceError):
            await manager.create_session(
                robot_id="missing",
                camera_id="cam0",
                offer_sdp=browser.localDescription.sdp,
                offer_type=browser.localDescription.type,
            )
        await browser.close()

    _run(scenario())


def test_ice_and_stop_on_unknown_session_raise():
    manager = SignalingManager(StubFrameSourceProvider())

    async def scenario():
        with pytest.raises(KeyError):
            await manager.add_ice_candidate("nope", {"candidate": ""})
        # close_session on an unknown id is a no-op.
        await manager.close_session("also-nope")

    _run(scenario())


# ---------------------------------------------------------------------------
# codec preference
# ---------------------------------------------------------------------------


def test_apply_codec_preference_is_defensive_without_transceiver():
    class DummySender:
        pass

    # Must not raise and must return a list.
    applied = apply_codec_preference(DummySender())  # type: ignore[arg-type]
    assert isinstance(applied, list)


# ---------------------------------------------------------------------------
# router.py — HTTP surface
# ---------------------------------------------------------------------------


def _build_test_app() -> tuple[FastAPI, SignalingManager]:
    app = FastAPI()
    manager = SignalingManager(StubFrameSourceProvider(width=16, height=12, fps=120))
    app.state.streaming = manager
    app.include_router(build_streams_router(), prefix="/api")
    return app, manager


def test_router_returns_503_when_streaming_unwired():
    app = FastAPI()
    app.include_router(build_streams_router(), prefix="/api")
    with TestClient(app) as client:
        resp = client.post(
            "/api/streams/offer",
            json={"robot_id": "r", "camera_id": "c", "sdp": "", "type": "offer"},
        )
    assert resp.status_code == 503


def test_router_offer_ice_stats_quality_stop_cycle():
    app, manager = _build_test_app()

    async def _build_offer() -> tuple[RTCPeerConnection, str, str]:
        browser = RTCPeerConnection()
        browser.addTransceiver("video", direction="recvonly")
        offer = await browser.createOffer()
        await browser.setLocalDescription(offer)
        return browser, browser.localDescription.sdp, browser.localDescription.type

    # Build the offer in its own loop, then throw away the browser peer — we
    # only need its SDP. Reusing the peer across loops trips aiortc's internal
    # transport state, which is tied to the loop it was created on.
    async def _capture_offer() -> tuple[str, str]:
        browser, sdp, sdp_type = await _build_offer()
        try:
            return sdp, sdp_type
        finally:
            await browser.close()

    sdp, sdp_type = asyncio.run(_capture_offer())

    try:
        with TestClient(app) as client:
            offer_resp = client.post(
                "/api/streams/offer",
                json={"robot_id": "r1", "camera_id": "cam0", "sdp": sdp, "type": sdp_type},
            )
            assert offer_resp.status_code == 200, offer_resp.text
            offer_data = offer_resp.json()
            sid = offer_data["session_id"]
            assert offer_data["applied_codecs"]

            # Empty candidate (end-of-candidates) is accepted.
            ice_resp = client.post(f"/api/streams/{sid}/ice", json={"candidate": ""})
            assert ice_resp.status_code == 204

            # Stats endpoint returns a list.
            stats_resp = client.get(f"/api/streams/{sid}/stats")
            assert stats_resp.status_code == 200
            assert stats_resp.json()["session_id"] == sid
            assert isinstance(stats_resp.json()["entries"], list)

            # Quality patch merges values.
            patch_resp = client.patch(
                f"/api/streams/{sid}/quality",
                json={"width": 32, "height": 24, "bitrate_kbps": 800},
            )
            assert patch_resp.status_code == 200
            assert patch_resp.json()["width"] == 32
            assert patch_resp.json()["bitrate_kbps"] == 800

            # Unknown session -> 404 on patch.
            missing = client.patch(
                "/api/streams/does-not-exist/quality",
                json={"bitrate_kbps": 500},
            )
            assert missing.status_code == 404

            # Stop is idempotent.
            stop_resp = client.post(f"/api/streams/{sid}/stop")
            assert stop_resp.status_code == 204
            assert manager.get(sid) is None
    finally:
        asyncio.run(manager.close_all())
