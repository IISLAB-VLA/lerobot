"""RecorderService lifecycle tests (task #13 phase 1a)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from lerobot.dashboard.services.camera_manager import InMemoryCameraManager
from lerobot.dashboard.services.recorder import (
    ProgressEvent,
    RecorderConflictError,
    RecorderNotFoundError,
    RecorderService,
    RecorderValidationError,
    StartRequest,
)
from lerobot.dashboard.services.registry import Registry, registry_path_for
from lerobot.dashboard.services.registry_models import (
    CameraEntry,
    CameraSource,
    RobotEntry,
    SerialConnection,
)
from lerobot.dashboard.services.robot_manager import InMemoryRobotManager


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_path_for(tmp_path / "reg"))


@pytest.fixture
def robot_manager() -> InMemoryRobotManager:
    return InMemoryRobotManager()


@pytest.fixture
def camera_manager() -> InMemoryCameraManager:
    return InMemoryCameraManager()


@pytest.fixture
def datasets_dir(tmp_path: Path) -> Path:
    return tmp_path / "datasets"


async def _camera(registry: Registry, name: str = "front") -> CameraEntry:
    return await registry.create_camera(
        CameraEntry(
            name=name,
            backend="opencv",
            source=CameraSource(index=0),
            width=64,
            height=48,
            fps=15,
        )
    )


async def _robot(registry: Registry, camera_ids: list | None = None) -> RobotEntry:
    return await registry.create_robot(
        RobotEntry(
            name="so101",
            robot_type="so101_follower",
            connection=SerialConnection(port="/dev/ttyUSB0", baudrate=1_000_000),
            cameras=camera_ids or [],
        )
    )


async def _make_service(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> RecorderService:
    return RecorderService(
        registry=registry,
        robot_manager=robot_manager,
        camera_manager=camera_manager,
        datasets_dir=datasets_dir,
    )


async def test_start_refuses_when_robot_not_connected(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    robot = await _robot(registry)
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    req = StartRequest(
        robot_id=robot.id,
        dataset_name="pilot",
        task_description="test",
        fps=15,
    )
    with pytest.raises(RecorderValidationError):
        await svc.start(req)


async def test_start_refuses_bad_dataset_name(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    # Pydantic enforces min_length/max_length; the recorder enforces the shape
    # regex (lowercase + no path separators, must start with [a-z0-9]).
    for bad in ("UPPER", "has space", "slash/here", "-leading"):
        with pytest.raises(RecorderValidationError):
            await svc.start(
                StartRequest(
                    robot_id=robot.id,
                    dataset_name=bad,
                    task_description="test",
                    fps=10,
                )
            )


async def test_start_creates_dataset_and_enters_recording(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    cam = await _camera(registry)
    robot = await _robot(registry, camera_ids=[cam.id])
    await robot_manager.connect(robot, [cam])
    await camera_manager.open(cam)
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    req = StartRequest(
        robot_id=robot.id,
        dataset_name="pilot_v1",
        task_description="hello",
        fps=10,
    )
    session = await svc.start(req)
    try:
        assert session.status == "recording"
        assert session.dataset_name == "pilot_v1"
        assert Path(session.dataset_path).is_dir()
    finally:
        await svc.stop(session.id, save=False)


async def test_start_refuses_duplicate_per_robot(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    s1 = await svc.start(StartRequest(robot_id=robot.id, dataset_name="a", task_description="t", fps=10))
    try:
        with pytest.raises(RecorderConflictError):
            await svc.start(StartRequest(robot_id=robot.id, dataset_name="b", task_description="t", fps=10))
    finally:
        await svc.stop(s1.id, save=False)


async def test_stop_discard_marks_discarded(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    session = await svc.start(StartRequest(robot_id=robot.id, dataset_name="c", task_description="t", fps=10))
    summary = await svc.stop(session.id, save=False)
    assert summary.status == "discarded"
    assert summary.saved is False
    assert summary.stopped_at is not None


async def test_stop_unknown_raises_not_found(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    with pytest.raises(RecorderNotFoundError):
        await svc.stop(uuid4(), save=True)


async def test_stop_twice_raises_conflict(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    session = await svc.start(StartRequest(robot_id=robot.id, dataset_name="d", task_description="t", fps=10))
    await svc.stop(session.id, save=False)
    with pytest.raises(RecorderConflictError):
        await svc.stop(session.id, save=False)


async def test_list_reflects_created_sessions(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    assert await svc.list() == []
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    session = await svc.start(StartRequest(robot_id=robot.id, dataset_name="e", task_description="t", fps=10))
    try:
        listed = await svc.list()
        assert [s.id for s in listed] == [session.id]
    finally:
        await svc.stop(session.id, save=False)


async def test_subscribe_yields_terminal_event_for_finished_session(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    session = await svc.start(StartRequest(robot_id=robot.id, dataset_name="f", task_description="t", fps=10))
    await svc.stop(session.id, save=False)
    events: list[ProgressEvent] = []
    async for event in svc.subscribe(session.id):
        events.append(event)
    assert events and events[-1].type in {"stopped", "error"}


async def test_close_stops_active_sessions(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    session = await svc.start(StartRequest(robot_id=robot.id, dataset_name="g", task_description="t", fps=10))
    await svc.close()
    final = await svc.get(session.id)
    assert final.status in {"discarded", "failed"}


async def test_start_after_previous_session_saved(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    """A terminated session must not block a new one on the same robot."""
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    first = await svc.start(StartRequest(robot_id=robot.id, dataset_name="h", task_description="t", fps=10))
    await svc.stop(first.id, save=False)
    second = await svc.start(StartRequest(robot_id=robot.id, dataset_name="i", task_description="t", fps=10))
    try:
        assert second.id != first.id
        assert second.status == "recording"
    finally:
        await svc.stop(second.id, save=False)


async def test_capture_loop_increments_frame_counter(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
    datasets_dir: Path,
) -> None:
    """In fake-devices mode add_frame may drop, but the counter still advances
    (either frames or drops)."""
    robot = await _robot(registry)
    await robot_manager.connect(robot, [])
    svc = await _make_service(registry, robot_manager, camera_manager, datasets_dir)
    session = await svc.start(StartRequest(robot_id=robot.id, dataset_name="j", task_description="t", fps=30))
    try:
        # Let the loop tick a handful of times.
        await asyncio.sleep(0.2)
        snapshot = await svc.get(session.id)
        assert (snapshot.frames_captured + snapshot.drop_count) > 0
    finally:
        await svc.stop(session.id, save=False)
