"""Protocol + in-memory fallback tests for camera/teleop managers."""

from __future__ import annotations

from uuid import uuid4

import numpy as np
import pytest

from lerobot.dashboard.services.camera_manager import (
    CameraManagerProtocol,
    InMemoryCameraManager,
)
from lerobot.dashboard.services.registry_models import (
    CameraEntry,
    CameraSource,
    TeleopEntry,
)
from lerobot.dashboard.services.teleop_manager import (
    InMemoryTeleopManager,
    TeleopManagerProtocol,
)
from lerobot.dashboard.teleop.protocol import (
    GamepadEvent,
    KeyboardEvent,
    parse_teleop_event,
)


def _camera() -> CameraEntry:
    return CameraEntry(
        name="front",
        backend="opencv",
        source=CameraSource(index=0),
        width=64,
        height=48,
        fps=30,
    )


def _teleop() -> TeleopEntry:
    return TeleopEntry(name="kb", kind="keyboard")


def test_in_memory_camera_manager_matches_protocol() -> None:
    assert isinstance(InMemoryCameraManager(), CameraManagerProtocol)


def test_in_memory_teleop_manager_matches_protocol() -> None:
    assert isinstance(InMemoryTeleopManager(), TeleopManagerProtocol)


async def test_camera_open_close_status_cycle() -> None:
    mgr = InMemoryCameraManager()
    entry = _camera()
    assert await mgr.is_open(entry.id) is False

    opened = await mgr.open(entry)
    assert opened.online is True
    assert opened.opened_at is not None
    assert await mgr.is_open(entry.id) is True

    closed = await mgr.close(entry.id)
    assert closed.online is False
    assert await mgr.is_open(entry.id) is False


async def test_camera_subscribe_yields_frames_of_declared_shape() -> None:
    mgr = InMemoryCameraManager()
    entry = _camera()
    await mgr.open(entry)

    frames: list[np.ndarray] = []
    it = mgr.subscribe(entry.id)
    async for frame in it:
        frames.append(frame)
        if len(frames) == 2:
            break
    await it.aclose()

    assert all(f.shape == (entry.height, entry.width, 3) for f in frames)
    assert all(f.dtype == np.uint8 for f in frames)


async def test_camera_subscribe_tracks_subscriber_count() -> None:
    mgr = InMemoryCameraManager()
    entry = _camera()
    await mgr.open(entry)

    it = mgr.subscribe(entry.id)
    # Pull one frame so the body of the async generator actually runs
    # the _increment step before we inspect status.
    async for _ in it:
        break
    status = await mgr.get_status(entry.id)
    assert status.subscriber_count == 1

    await it.aclose()
    status = await mgr.get_status(entry.id)
    assert status.subscriber_count == 0


async def test_camera_subscribe_closed_camera_returns_empty() -> None:
    mgr = InMemoryCameraManager()
    # Never opened.
    frames: list[np.ndarray] = [f async for f in mgr.subscribe(uuid4())]
    assert frames == []


async def test_teleop_attach_detach_status_cycle() -> None:
    mgr = InMemoryTeleopManager()
    entry = _teleop()
    robot_id = uuid4()

    attached = await mgr.attach(entry, robot_id)
    assert attached.attached is True
    assert attached.bound_robot_id == robot_id
    assert await mgr.is_attached(entry.id) is True

    detached = await mgr.detach(entry.id)
    assert detached.attached is False
    assert detached.bound_robot_id is None


async def test_teleop_handle_input_records_while_attached() -> None:
    mgr = InMemoryTeleopManager()
    entry = _teleop()
    await mgr.attach(entry, uuid4())

    await mgr.handle_input(entry.id, KeyboardEvent(key="w", pressed=True))
    await mgr.handle_input(entry.id, GamepadEvent(axes=(0.1, -0.2), buttons=(True, False)))

    events = await mgr.recent_events(entry.id)
    match events:
        case [KeyboardEvent(key="w", pressed=True), GamepadEvent()]:
            pass
        case _:
            raise AssertionError(f"unexpected event sequence: {events}")


async def test_teleop_handle_input_dropped_when_detached() -> None:
    mgr = InMemoryTeleopManager()
    entry = _teleop()
    # Not attached — event must be dropped silently.
    await mgr.handle_input(entry.id, KeyboardEvent(key="a", pressed=True))
    assert await mgr.recent_events(entry.id) == []


async def test_unknown_ids_return_default_status() -> None:
    cam = InMemoryCameraManager()
    tel = InMemoryTeleopManager()
    ghost = uuid4()

    cs = await cam.get_status(ghost)
    assert cs.online is False
    assert cs.subscriber_count == 0

    ts = await tel.get_status(ghost)
    assert ts.attached is False
    assert ts.bound_robot_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "keyboard", "key": "w", "pressed": True},
        {"kind": "gamepad", "axes": [0.0, 1.0], "buttons": [True]},
    ],
)
def test_parse_teleop_event_selects_concrete_type(payload: dict) -> None:
    """``parse_teleop_event`` dispatches on the ``kind`` discriminator."""
    event = parse_teleop_event(payload)
    assert event.kind.value == payload["kind"]
