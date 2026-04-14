"""Smoke tests for the scratch WebRTC prototype (Task #9 pre-work).

These do not exercise the eventual src/lerobot/dashboard/streaming package;
they verify that the aiortc building blocks we plan to depend on behave as
expected in this environment. Once Task #6 and Task #2 unblock real code,
the equivalent tests will move into tests/dashboard/streaming/.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("aiortc")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scratch.streaming_prototype import (  # noqa: E402
    CODEC_PREFERENCE,
    LeRobotCameraTrack,
    SyntheticFrameSource,
    handshake,
    iter_stats,
    prefer_codecs,
)


@pytest.mark.asyncio
async def test_track_recv_converts_bgr_ndarray_to_videoframe():
    source = SyntheticFrameSource(width=32, height=24, fps=120)
    track = LeRobotCameraTrack(source, width=32, height=24)

    frame = await track.recv()

    assert frame.width == 32
    assert frame.height == 24
    assert frame.format.name == "bgr24"
    assert frame.pts is not None
    assert frame.time_base is not None


@pytest.mark.asyncio
async def test_track_recv_is_monotonic():
    source = SyntheticFrameSource(width=16, height=16, fps=240)
    track = LeRobotCameraTrack(source, width=16, height=16)

    first = await track.recv()
    second = await track.recv()

    assert second.pts > first.pts


@pytest.mark.asyncio
async def test_track_falls_back_to_black_on_source_failure():
    async def failing_source():
        raise RuntimeError("camera dropped")

    track = LeRobotCameraTrack(failing_source, width=8, height=8)
    frame = await track.recv()
    assert frame.width == 8
    assert frame.height == 8
    # Black frame: every pixel channel is zero.
    ndarray = frame.to_ndarray(format="bgr24")
    assert np.all(ndarray == 0)


@pytest.mark.asyncio
async def test_handshake_completes_and_applies_preference():
    source = SyntheticFrameSource(width=16, height=16, fps=120)
    track = LeRobotCameraTrack(source, width=16, height=16)

    offerer, answerer, applied = await handshake(track)
    try:
        assert offerer.localDescription is not None
        assert answerer.localDescription is not None
        # At least one preferred codec must be advertised by aiortc in the test env,
        # otherwise the production path cannot satisfy the <200ms latency budget.
        assert applied, "aiortc did not expose any of the preferred codecs"
        for mime in applied:
            assert mime in CODEC_PREFERENCE
    finally:
        await offerer.close()
        await answerer.close()


@pytest.mark.asyncio
async def test_iter_stats_yields_report_entries():
    source = SyntheticFrameSource(width=16, height=16, fps=120)
    track = LeRobotCameraTrack(source, width=16, height=16)

    offerer, answerer, _ = await handshake(track)
    try:
        # Give the PC a moment to populate at least one stats entry.
        await asyncio.sleep(0.2)
        entries = [entry async for entry in iter_stats(offerer)]
        assert entries, "getStats() returned no entries"
        assert all("type" in entry for entry in entries)
    finally:
        await offerer.close()
        await answerer.close()


def test_prefer_codecs_is_defensive_when_called_bare():
    """Calling prefer_codecs without an active transceiver should not raise."""

    class DummySender:
        pass

    applied = prefer_codecs(DummySender())
    # Returns either the preferred codec list or an empty list; both are fine.
    assert isinstance(applied, list)
