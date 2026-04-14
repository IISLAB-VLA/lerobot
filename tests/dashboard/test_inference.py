"""InferenceService lifecycle + policy loader tests (task #14 phase 1a)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from lerobot.dashboard.services.camera_manager import InMemoryCameraManager
from lerobot.dashboard.services.inference import (
    InferenceConflictError,
    InferenceNotFoundError,
    InferenceService,
    InferenceValidationError,
    PolicyCache,
    PolicyDescriptor,
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


class StubPolicy:
    """Tiny stand-in for PreTrainedPolicy used to exercise the service without
    loading real weights. Records every forward call for assertions."""

    def __init__(self, action_features: dict[str, type] | None = None) -> None:
        self.action_features = action_features or {"joint_0": float, "joint_1": float}
        self.select_action_calls: list[tuple[dict, str | None]] = []
        self.action_value = [0.1, -0.2]

    def eval(self) -> None:  # parity with PreTrainedPolicy
        pass

    def select_action(self, obs: dict, task: str | None = None):  # noqa: D401
        self.select_action_calls.append((obs, task))
        return self.action_value


def _camera_entry(name: str = "front") -> CameraEntry:
    return CameraEntry(
        name=name,
        backend="opencv",
        source=CameraSource(index=0),
        width=16,
        height=12,
        fps=15,
    )


def _robot_entry(camera_ids: list | None = None) -> RobotEntry:
    return RobotEntry(
        name="so101",
        robot_type="so101_follower",
        connection=SerialConnection(port="/dev/ttyUSB0", baudrate=1_000_000),
        cameras=camera_ids or [],
    )


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
def service_factory(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
):
    """Returns a callable ``(stub_policies={...})`` → InferenceService.

    Wraps the descriptor cache and loader so tests don't touch the real HF
    cache. The service is returned along with a helper to register stub
    descriptors for start().
    """
    made: list[InferenceService] = []

    def _make(stub: StubPolicy | None = None, repo_id: str = "lerobot/test-policy") -> InferenceService:
        svc = InferenceService(
            registry=registry,
            robot_manager=robot_manager,
            camera_manager=camera_manager,
            policy_loader=lambda _rid: stub or StubPolicy(),
        )
        # Pre-populate the descriptor cache so start() doesn't try to hit HF.
        descriptor = PolicyDescriptor(
            repo_id=repo_id,
            policy_type="act",
            root="/tmp/stub",
            supports_language=False,
        )
        svc._descriptor_cache = (float("inf"), [descriptor])  # type: ignore[attr-defined]
        made.append(svc)
        return svc

    yield _make


async def test_list_policies_returns_cached_descriptor(service_factory) -> None:
    svc = service_factory()
    descriptors = await svc.list_policies()
    assert [d.repo_id for d in descriptors] == ["lerobot/test-policy"]


async def test_get_policy_unknown_raises_not_found(service_factory) -> None:
    svc = service_factory()
    with pytest.raises(InferenceNotFoundError):
        await svc.get_policy("does/not-exist")


async def test_start_refuses_when_robot_not_connected(registry: Registry, service_factory) -> None:
    robot = await registry.create_robot(_robot_entry())
    svc = service_factory()
    with pytest.raises(InferenceValidationError):
        await svc.start(
            StartRequest(
                robot_id=robot.id,
                repo_id="lerobot/test-policy",
                fps=10,
            )
        )


async def test_start_refuses_unknown_policy(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    svc = service_factory()
    with pytest.raises(InferenceNotFoundError):
        await svc.start(
            StartRequest(
                robot_id=robot.id,
                repo_id="does/not-cached",
                fps=10,
            )
        )


async def test_start_rejects_bad_repo_id(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    svc = service_factory()
    with pytest.raises(InferenceValidationError):
        await svc.start(
            StartRequest(
                robot_id=robot.id,
                repo_id="contains spaces",
                fps=10,
            )
        )


async def test_start_enters_running_and_lists_session(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    stub = StubPolicy()
    svc = service_factory(stub=stub)
    session = await svc.start(
        StartRequest(
            robot_id=robot.id,
            repo_id="lerobot/test-policy",
            fps=10,
            task_description="move left",
            dry_run=True,
        )
    )
    try:
        assert session.status == "running"
        assert session.task_description == "move left"
        listed = await svc.list()
        assert [s.id for s in listed] == [session.id]
    finally:
        await svc.stop(session.id)


async def test_duplicate_session_per_robot_raises_conflict(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    svc = service_factory()
    session = await svc.start(StartRequest(robot_id=robot.id, repo_id="lerobot/test-policy", fps=10))
    try:
        with pytest.raises(InferenceConflictError):
            await svc.start(StartRequest(robot_id=robot.id, repo_id="lerobot/test-policy", fps=10))
    finally:
        await svc.stop(session.id)


async def test_stop_twice_raises_conflict(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    svc = service_factory()
    session = await svc.start(StartRequest(robot_id=robot.id, repo_id="lerobot/test-policy", fps=10))
    await svc.stop(session.id)
    with pytest.raises(InferenceConflictError):
        await svc.stop(session.id)


async def test_stop_unknown_raises_not_found(service_factory) -> None:
    svc = service_factory()
    with pytest.raises(InferenceNotFoundError):
        await svc.stop(uuid4())


async def test_set_command_updates_task_description(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    svc = service_factory()
    session = await svc.start(
        StartRequest(
            robot_id=robot.id,
            repo_id="lerobot/test-policy",
            fps=10,
            task_description="pick cube",
        )
    )
    try:
        updated = await svc.set_command(session.id, "place cube")
        assert updated.task_description == "place cube"
        fetched = await svc.get(session.id)
        assert fetched.task_description == "place cube"
    finally:
        await svc.stop(session.id)


async def test_subscribe_yields_terminal_event_for_finished_session(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    svc = service_factory()
    session = await svc.start(StartRequest(robot_id=robot.id, repo_id="lerobot/test-policy", fps=10))
    await svc.stop(session.id)
    events = [e async for e in svc.subscribe(session.id)]
    assert events and events[-1].type == "stopped"


async def test_capture_loop_invokes_policy_and_emits_step_events(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    stub = StubPolicy()
    svc = service_factory(stub=stub)
    session = await svc.start(
        StartRequest(
            robot_id=robot.id,
            repo_id="lerobot/test-policy",
            fps=30,
            dry_run=True,
        )
    )
    try:
        await asyncio.sleep(0.15)
        # The loop should have made at least one policy call and incremented
        # the step counter (dry-run means no send_action is attempted).
        assert len(stub.select_action_calls) >= 1
        snapshot = await svc.get(session.id)
        assert snapshot.step >= 1
    finally:
        await svc.stop(session.id)


async def test_policy_cache_lru_evicts_oldest() -> None:
    created: list[str] = []

    def loader(repo_id: str) -> str:
        created.append(repo_id)
        return f"policy-{repo_id}"

    cache = PolicyCache(loader, max_resident=2)
    await cache.get("a")
    await cache.get("b")
    await cache.get("a")  # refresh recency
    await cache.get("c")  # should evict 'b'
    await cache.get("b")  # reload triggers loader again
    assert created == ["a", "b", "c", "b"]


async def test_start_rejects_policy_with_missing_action_keys(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    """Robot with populated action features + policy that needs more keys
    must fail at ``start`` with 422 rather than blowing up mid-loop."""

    async def get_features(_robot_id):
        return {
            "observation": {"observation.state": float},
            "action": {"joint_0": float},
        }

    robot_manager.get_features = get_features  # type: ignore[assignment]
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    # Stub declares an extra 'joint_1' the robot doesn't have.
    stub = StubPolicy(action_features={"joint_0": float, "joint_1": float})
    stub.observation_features = {"observation.state": float}  # type: ignore[attr-defined]
    svc = service_factory(stub=stub)
    with pytest.raises(InferenceValidationError):
        await svc.start(StartRequest(robot_id=robot.id, repo_id="lerobot/test-policy", fps=10))


async def test_start_rejects_policy_with_missing_observation_keys(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    async def get_features(_robot_id):
        return {
            "observation": {"observation.state": float},
            "action": {"joint_0": float},
        }

    robot_manager.get_features = get_features  # type: ignore[assignment]
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    stub = StubPolicy(action_features={"joint_0": float})
    # Policy expects a camera the robot doesn't have.
    stub.config = type(
        "Cfg",
        (),
        {"input_features": {"observation.state": float, "observation.images.front": (64, 64, 3)}},
    )()
    svc = service_factory(stub=stub)
    with pytest.raises(InferenceValidationError):
        await svc.start(StartRequest(robot_id=robot.id, repo_id="lerobot/test-policy", fps=10))


async def test_non_dry_run_calls_send_action(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    service_factory,
) -> None:
    """With dry_run=False the loop forwards the policy output to the robot."""
    recorded: list[dict[str, float]] = []

    async def spy(robot_id, action):
        recorded.append(dict(action))

    robot_manager.send_action = spy  # type: ignore[assignment]
    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])
    stub = StubPolicy(action_features={"joint_0": float, "joint_1": float})
    svc = service_factory(stub=stub)
    session = await svc.start(
        StartRequest(
            robot_id=robot.id,
            repo_id="lerobot/test-policy",
            fps=30,
            dry_run=False,
        )
    )
    try:
        await asyncio.sleep(0.15)
        assert recorded, "send_action never called"
        assert set(recorded[0].keys()) == {"joint_0", "joint_1"}
    finally:
        await svc.stop(session.id)
