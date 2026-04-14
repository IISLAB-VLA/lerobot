"""LerobotCameraManager tests.

Cameras are stubbed with a :class:`_FakeCamera` so the suite runs without
any actual hardware. Covers lifecycle, fan-out to multiple subscribers,
graceful close, fatal capture error, and the close/subscribe race.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

import numpy as np
import pytest

from lerobot.dashboard.services.camera_manager import (
    CameraManagerProtocol,
    LerobotCameraManager,
)
from lerobot.dashboard.services.registry_models import CameraEntry, CameraSource, CameraStatus
from lerobot.dashboard.streaming.source import FrameSourceError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCamera:
    """Minimal :class:`lerobot.cameras.Camera` substitute.

    Produces monotonically-numbered gradient frames; can be steered to
    raise on demand to exercise the fatal-error path.
    """

    height: int = 16
    width: int = 16
    fps: int = 30
    connected: bool = False
    raise_on_read: BaseException | None = None
    _counter: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _step: asyncio.Event | None = field(default=None, init=False)

    @property
    def is_connected(self) -> bool:
        return self.connected

    def connect(self, warmup: bool = True) -> None:  # noqa: ARG002
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def async_read(self, timeout_ms: float = 200) -> np.ndarray:  # noqa: ARG002
        with self._lock:
            if self.raise_on_read is not None:
                raise self.raise_on_read
            self._counter += 1
            n = self._counter
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:, :, 0] = n % 256
        return frame


def _entry(name: str = "cam0") -> CameraEntry:
    return CameraEntry(
        name=name,
        backend="opencv",
        source=CameraSource(index=0),
        width=16,
        height=16,
        fps=30,
    )


def _manager_with(camera: _FakeCamera) -> LerobotCameraManager:
    return LerobotCameraManager(camera_builder=lambda entry: camera)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_satisfies_protocol() -> None:
    assert isinstance(LerobotCameraManager(), CameraManagerProtocol)


async def test_open_connects_camera_and_marks_online() -> None:
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()

    await mgr.open(entry)

    assert cam.connected is True
    assert await mgr.is_open(entry.id) is True
    status = await mgr.get_status(entry.id)
    assert status.online is True
    assert status.onlineed_at is not None

    await mgr.close(entry.id)
    assert cam.connected is False
    assert await mgr.is_open(entry.id) is False


async def test_subscribe_yields_bgr_frames() -> None:
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()
    await mgr.open(entry)

    gen = mgr.subscribe(entry.id)
    frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert frame.shape == (16, 16, 3)
    assert frame.dtype == np.uint8

    await gen.aclose()
    await mgr.close(entry.id)


async def test_fan_out_broadcasts_to_all_subscribers() -> None:
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()
    await mgr.open(entry)

    gen_a = mgr.subscribe(entry.id)
    gen_b = mgr.subscribe(entry.id)
    a = await asyncio.wait_for(gen_a.__anext__(), timeout=1.0)
    b = await asyncio.wait_for(gen_b.__anext__(), timeout=1.0)
    assert a.shape == b.shape

    status = await mgr.get_status(entry.id)
    assert status.subscriber_count == 2

    await gen_a.aclose()
    await gen_b.aclose()
    await mgr.close(entry.id)

    status_after = await mgr.get_status(entry.id)
    assert status_after.online is False


async def test_graceful_close_ends_subscriber_without_error() -> None:
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()
    await mgr.open(entry)

    gen = mgr.subscribe(entry.id)
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    async def consume_until_end() -> int:
        count = 0
        async for _ in gen:
            count += 1
        return count

    consumer = asyncio.create_task(consume_until_end())
    await asyncio.sleep(0.05)
    await mgr.close(entry.id)

    total = await asyncio.wait_for(consumer, timeout=1.0)
    assert isinstance(total, int)


async def test_fatal_capture_error_propagates_as_frame_source_error() -> None:
    boom = RuntimeError("cable yanked")
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()
    await mgr.open(entry)

    # Let one good frame through before the error.
    gen = mgr.subscribe(entry.id)
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    cam.raise_on_read = boom

    with pytest.raises(FrameSourceError, match="cable yanked"):
        while True:
            await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    status = await mgr.get_status(entry.id)
    assert status.online is False
    assert status.last_error == "cable yanked"
    await mgr.close(entry.id)


async def test_subscribe_to_unknown_camera_raises() -> None:
    mgr = LerobotCameraManager()
    entry = _entry()
    gen = mgr.subscribe(entry.id)
    with pytest.raises(FrameSourceError, match="not open"):
        await gen.__anext__()


async def test_open_failure_raises_frame_source_error() -> None:
    class _BrokenCamera(_FakeCamera):
        def connect(self, warmup: bool = True) -> None:  # noqa: ARG002
            raise OSError("no camera")

    broken = _BrokenCamera()
    mgr = _manager_with(broken)
    with pytest.raises(FrameSourceError, match="connect failed"):
        await mgr.open(_entry())


async def test_double_open_is_idempotent() -> None:
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()
    await mgr.open(entry)
    await mgr.open(entry)  # should not create a second slot / reconnect
    status = await mgr.get_status(entry.id)
    assert status.online is True
    assert status.subscriber_count == 0
    await mgr.close(entry.id)


async def test_slow_consumer_gets_latest_frame_not_backlog() -> None:
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()
    await mgr.open(entry)

    gen = mgr.subscribe(entry.id)
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    # Give the producer time to generate more frames than the queue can hold.
    await asyncio.sleep(0.15)
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert int(first[0, 0, 0]) <= int(second[0, 0, 0])
    # No unbounded queue growth — even with a long sleep only one frame is buffered.
    await gen.aclose()
    await mgr.close(entry.id)


async def test_status_tracks_subscriber_count() -> None:
    cam = _FakeCamera()
    mgr = _manager_with(cam)
    entry = _entry()
    await mgr.open(entry)

    gen = mgr.subscribe(entry.id)
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    status = await mgr.get_status(entry.id)
    assert status.subscriber_count == 1
    await gen.aclose()
    await mgr.close(entry.id)


def test_camera_status_defaults_are_offline() -> None:
    s = CameraStatus()
    assert s.online is False
    assert s.opened_at is None
    assert s.last_error is None
    assert s.subscriber_count == 0
