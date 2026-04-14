"""Calibration controller tests.

Covers:
* Step plan dispatch per robot_type.
* Session lifecycle — start, ack-through, done.
* Cancel path — partial sessions.
* REST + WS contract via FastAPI TestClient.

A minimal :class:`_FakeRobotManager` drives the controller without needing
an actual :class:`InMemoryRobotManager` instance — keeps the tests focused.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.app import create_app  # noqa: E402
from lerobot.dashboard.core.config import DashboardConfig  # noqa: E402
from lerobot.dashboard.services.calibration import (  # noqa: E402
    CalibrationController,
    RobotNotConnectedError,
    SessionAlreadyActiveError,
    SessionMismatchError,
    SessionNotFoundError,
    default_step_plan,
)
from lerobot.dashboard.services.registry_models import (  # noqa: E402
    RobotEntry,
    SerialConnection,
)
from lerobot.dashboard.services.robot_manager import RobotStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRobotManager:
    def __init__(self, *, online: bool = True) -> None:
        self._online = online

    async def is_connected(self, robot_id: UUID) -> bool:
        return self._online

    async def read_observation(self, robot_id: UUID) -> dict[str, Any]:
        return {"joint_1.pos": 0.11, "joint_2.pos": -0.22}

    # The other RobotManagerProtocol methods aren't exercised by the
    # controller so we leave them as no-ops.
    async def connect(self, entry: RobotEntry, cameras: list[Any]) -> RobotStatus:  # pragma: no cover
        return RobotStatus(online=True)

    async def disconnect(self, robot_id: UUID) -> RobotStatus:  # pragma: no cover
        return RobotStatus()

    async def get_status(self, robot_id: UUID) -> RobotStatus:  # pragma: no cover
        return RobotStatus(online=self._online)

    async def send_action(self, robot_id: UUID, action: dict[str, float]) -> None:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Step plan
# ---------------------------------------------------------------------------


def test_step_plan_maps_known_types() -> None:
    so_plan = default_step_plan("so101_follower")
    assert len(so_plan) == 5
    assert so_plan[0].step_id == "home_pose"

    ur_plan = default_step_plan("ur")
    assert len(ur_plan) == 2
    assert ur_plan[-1].step_id == "verify_payload"


def test_step_plan_falls_back_for_unknown_type() -> None:
    plan = default_step_plan("experimental_x_arm")
    assert len(plan) == 1
    assert plan[0].step_id == "manual"


# ---------------------------------------------------------------------------
# Controller state machine
# ---------------------------------------------------------------------------


async def _drain_subscribe(
    controller: CalibrationController, robot_id: UUID, session_id: str
) -> AsyncIterator[dict[str, Any]]:
    """Yield events from the controller, stopping when the generator closes."""
    async for event in controller.subscribe(robot_id, session_id):
        yield event


async def test_start_rejects_offline_robot() -> None:
    controller = CalibrationController()
    mgr = _FakeRobotManager(online=False)
    with pytest.raises(RobotNotConnectedError):
        await controller.start(uuid4(), "so101_follower", mgr)


async def test_start_conflicts_with_existing_session() -> None:
    controller = CalibrationController()
    mgr = _FakeRobotManager()
    robot_id = uuid4()
    first = await controller.start(robot_id, "ur", mgr)
    with pytest.raises(SessionAlreadyActiveError) as exc:
        await controller.start(robot_id, "ur", mgr)
    assert exc.value.existing_session_id == first.session_id
    await controller.cancel(robot_id, first.session_id)


async def test_ack_advances_through_full_plan() -> None:
    controller = CalibrationController()
    mgr = _FakeRobotManager()
    robot_id = uuid4()
    start = await controller.start(robot_id, "ur", mgr)
    plan = default_step_plan("ur")

    collected: list[dict[str, Any]] = []

    async def _consume() -> None:
        async for event in controller.subscribe(robot_id, start.session_id):
            collected.append(event)
            if event["type"] == "done":
                return

    consumer = asyncio.create_task(_consume())
    # Yield so the consumer registers its subscription before we ack any step.
    await asyncio.sleep(0)
    try:
        for step in plan:
            # Wait until the server publishes the step and is awaiting input.
            for _ in range(50):
                snapshot = await controller.get_status(robot_id)
                if (
                    snapshot is not None
                    and snapshot.step_id == step.step_id
                    and snapshot.awaiting_user_input
                ):
                    break
                await asyncio.sleep(0.01)
            else:  # pragma: no cover - progress timeout
                pytest.fail(f"never reached step {step.step_id}")
            await controller.ack(robot_id, start.session_id, step.step_id)
        await asyncio.wait_for(consumer, timeout=2.0)
    finally:
        if not consumer.done():
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, Exception):
                pass

    types = [e["type"] for e in collected]
    assert types[0] == "step"
    assert types[-1] == "done"
    assert collected[-1]["result"] == "ok"

    # Summary is stored for GET /calibration.
    summary = await controller.latest(robot_id)
    assert summary is not None
    assert summary.result == "ok"
    assert summary.step_ids == [s.step_id for s in plan]


async def test_ack_mismatched_step_id_is_rejected() -> None:
    controller = CalibrationController()
    mgr = _FakeRobotManager()
    robot_id = uuid4()
    start = await controller.start(robot_id, "ur", mgr)
    for _ in range(50):
        snapshot = await controller.get_status(robot_id)
        if snapshot is not None and snapshot.awaiting_user_input:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(SessionMismatchError) as exc:
        await controller.ack(robot_id, start.session_id, "not_a_real_step")
    assert exc.value.current_step_id == "verify_tcp_offset"
    await controller.cancel(robot_id, start.session_id)


async def test_joint_feedback_is_published_while_session_runs() -> None:
    controller = CalibrationController()
    mgr = _FakeRobotManager()
    robot_id = uuid4()
    start = await controller.start(robot_id, "ur", mgr)

    async def _read() -> dict[str, Any]:
        async for event in controller.subscribe(robot_id, start.session_id):
            if event["type"] == "joint_feedback":
                return event
            if event["type"] == "done":
                raise AssertionError("session ended before joint_feedback arrived")
        raise AssertionError("subscription closed without joint_feedback")

    feedback = await asyncio.wait_for(_read(), timeout=1.0)
    assert feedback["step_id"] == "verify_tcp_offset"
    assert feedback["session_id"] == start.session_id
    assert isinstance(feedback["timestamp_ms"], int) and feedback["timestamp_ms"] > 0
    assert feedback["values"]["joint_1.pos"] == pytest.approx(0.11)
    assert "joint_2.pos" in feedback["values"]

    await controller.cancel(robot_id, start.session_id)


async def test_cancel_produces_cancelled_done_event() -> None:
    controller = CalibrationController()
    mgr = _FakeRobotManager()
    robot_id = uuid4()
    start = await controller.start(robot_id, "ur", mgr)

    events: list[dict[str, Any]] = []

    async def _consume() -> None:
        async for event in controller.subscribe(robot_id, start.session_id):
            events.append(event)
            if event["type"] == "done":
                return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)
    await controller.cancel(robot_id, start.session_id)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert events[-1]["type"] == "done"
    assert events[-1]["result"] == "cancelled"
    summary = await controller.latest(robot_id)
    assert summary is not None and summary.result == "cancelled"


async def test_cancel_nonexistent_session_raises() -> None:
    controller = CalibrationController()
    with pytest.raises(SessionNotFoundError):
        await controller.cancel(uuid4(), "cal-nope")


async def test_status_returns_none_when_no_session() -> None:
    controller = CalibrationController()
    assert await controller.get_status(uuid4()) is None


# ---------------------------------------------------------------------------
# REST contract
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = DashboardConfig(
        storage_dir=tmp_path / "storage",
        static_dir=None,
        fake_devices=True,
    )
    return TestClient(create_app(config))


def _create_robot(client: TestClient) -> str:
    payload = {
        "name": "so101",
        "robot_type": "so101_follower",
        "connection": {
            "kind": "serial",
            "port": "/dev/serial/by-id/usb-feetech-fake",
        },
    }
    resp = client.post("/api/robots", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_rest_start_requires_connected_robot(client: TestClient) -> None:
    robot_id = _create_robot(client)
    resp = client.post(f"/api/robots/{robot_id}/calibrate/start")
    assert resp.status_code == 409
    assert "not connected" in resp.json()["detail"]["detail"]


def test_rest_start_and_status_contract(client: TestClient) -> None:
    robot_id = _create_robot(client)
    # Connect via the normal API so InMemoryRobotManager marks it online.
    connect = client.post(f"/api/robots/{robot_id}/connect")
    assert connect.status_code == 200, connect.text

    started = client.post(f"/api/robots/{robot_id}/calibrate/start").json()
    assert started["robot_type"] == "so101_follower"
    assert started["total_steps"] == 5
    assert started["step_ids"][0] == "home_pose"

    # The session should show up on the status endpoint.
    status_resp = None
    for _ in range(30):
        status_resp = client.get(f"/api/robots/{robot_id}/calibrate/status")
        if status_resp.status_code == 200 and status_resp.json().get("awaiting_user_input"):
            break
    assert status_resp is not None and status_resp.status_code == 200
    snapshot = status_resp.json()
    assert snapshot["session_id"] == started["session_id"]
    assert snapshot["step_id"] == "home_pose"

    # Cancel to clean up the background task before the test client tears down.
    cancel = client.post(
        f"/api/robots/{robot_id}/calibrate/cancel",
        json={"session_id": started["session_id"]},
    )
    assert cancel.status_code == 202


def test_rest_status_404_when_no_session(client: TestClient) -> None:
    robot_id = _create_robot(client)
    resp = client.get(f"/api/robots/{robot_id}/calibrate/status")
    assert resp.status_code == 404


def test_rest_latest_404_when_no_summary(client: TestClient) -> None:
    robot_id = _create_robot(client)
    resp = client.get(f"/api/robots/{robot_id}/calibration")
    assert resp.status_code == 404


def test_rest_ack_conflict_for_bad_step(client: TestClient) -> None:
    robot_id = _create_robot(client)
    client.post(f"/api/robots/{robot_id}/connect")
    started = client.post(f"/api/robots/{robot_id}/calibrate/start").json()

    for _ in range(30):
        if client.get(f"/api/robots/{robot_id}/calibrate/status").json().get("awaiting_user_input"):
            break
    bad = client.post(
        f"/api/robots/{robot_id}/calibrate/ack",
        json={"session_id": started["session_id"], "step_id": "not_a_step"},
    )
    assert bad.status_code == 409
    detail = bad.json()["detail"]
    assert detail["current_step_id"] == "home_pose"

    client.post(
        f"/api/robots/{robot_id}/calibrate/cancel",
        json={"session_id": started["session_id"]},
    )


# ---------------------------------------------------------------------------
# WebSocket smoke
# ---------------------------------------------------------------------------


def test_ws_rejects_unknown_session(client: TestClient) -> None:
    robot_id = uuid4()
    with client.websocket_connect(
        f"/ws/robots/{robot_id}/calibrate?session_id=cal-nope"
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_ws_replays_current_step_on_connect(client: TestClient) -> None:
    robot_id = _create_robot(client)
    client.post(f"/api/robots/{robot_id}/connect")
    started = client.post(f"/api/robots/{robot_id}/calibrate/start").json()
    session_id = started["session_id"]

    # Wait until the first step is published.
    for _ in range(30):
        if client.get(f"/api/robots/{robot_id}/calibrate/status").json().get("awaiting_user_input"):
            break
    with client.websocket_connect(
        f"/ws/robots/{robot_id}/calibrate?session_id={session_id}"
    ) as ws:
        event = ws.receive_json()
        assert event["type"] == "step"
        assert event["step_id"] == "home_pose"

    client.post(
        f"/api/robots/{robot_id}/calibrate/cancel",
        json={"session_id": session_id},
    )
