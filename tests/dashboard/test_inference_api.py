"""HTTP + WS surface for /api/policies + /api/inference (task #14 phase 1b)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.app import create_app  # noqa: E402
from lerobot.dashboard.core.config import DashboardConfig  # noqa: E402
from lerobot.dashboard.services.inference import PolicyDescriptor  # noqa: E402


class StubPolicy:
    def __init__(self) -> None:
        self.action_features = {"joint_0": float, "joint_1": float}
        self.calls = 0

    def eval(self) -> None:  # noqa: D401
        pass

    def select_action(self, obs, task=None):  # noqa: D401
        self.calls += 1
        return [0.05, -0.05]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Dashboard TestClient with inference pre-populated with a stub policy."""
    config = DashboardConfig(
        storage_dir=tmp_path / "storage",
        static_dir=None,
        fake_devices=True,
    )
    app = create_app(config)
    svc = app.state.dashboard.inference
    # Short-circuit the HF cache scan + replace the loader with a stub so
    # the tests don't hit the network or load real weights.
    svc._descriptor_cache = (  # type: ignore[attr-defined]
        float("inf"),
        [
            PolicyDescriptor(
                repo_id="lerobot/test-policy",
                policy_type="act",
                root="/tmp/stub",
                supports_language=False,
            )
        ],
    )
    svc._policy_cache = type(svc._policy_cache)(lambda _rid: StubPolicy(), max_resident=2)
    return TestClient(app)


def _connected_robot(client: TestClient) -> dict:
    robot = client.post(
        "/api/robots",
        json={
            "name": "so101",
            "robot_type": "so101_follower",
            "connection": {
                "kind": "serial",
                "port": "/dev/ttyUSB0",
                "baudrate": 1_000_000,
            },
        },
    ).json()
    client.post(f"/api/robots/{robot['id']}/connect")
    return robot


def test_list_policies_returns_cached_descriptors(client: TestClient) -> None:
    body = client.get("/api/policies").json()
    assert [d["repo_id"] for d in body] == ["lerobot/test-policy"]


def test_get_policy_unknown_returns_404(client: TestClient) -> None:
    resp = client.get("/api/policies/does/not-exist")
    assert resp.status_code == 404


def test_list_inference_empty_by_default(client: TestClient) -> None:
    assert client.get("/api/inference").json() == []


def test_start_refuses_when_robot_not_connected(client: TestClient) -> None:
    robot = client.post(
        "/api/robots",
        json={
            "name": "so101",
            "robot_type": "so101_follower",
            "connection": {
                "kind": "serial",
                "port": "/dev/ttyUSB0",
                "baudrate": 1_000_000,
            },
        },
    ).json()
    resp = client.post(
        "/api/inference",
        json={
            "robot_id": robot["id"],
            "repo_id": "lerobot/test-policy",
            "fps": 10,
        },
    )
    assert resp.status_code == 422


def test_start_refuses_unknown_policy(client: TestClient) -> None:
    robot = _connected_robot(client)
    resp = client.post(
        "/api/inference",
        json={
            "robot_id": robot["id"],
            "repo_id": "does/not-cached",
            "fps": 10,
        },
    )
    assert resp.status_code == 404


def test_start_happy_path_and_lists_session(client: TestClient) -> None:
    robot = _connected_robot(client)
    start = client.post(
        "/api/inference",
        json={
            "robot_id": robot["id"],
            "repo_id": "lerobot/test-policy",
            "fps": 10,
            "task_description": "reach target",
            "dry_run": True,
        },
    )
    assert start.status_code == 202, start.text
    session = start.json()
    try:
        assert session["status"] == "running"
        listed = client.get("/api/inference").json()
        assert [s["id"] for s in listed] == [session["id"]]
    finally:
        client.post(f"/api/inference/{session['id']}/stop", json={})


def test_duplicate_session_per_robot_returns_409(client: TestClient) -> None:
    robot = _connected_robot(client)
    first = client.post(
        "/api/inference",
        json={
            "robot_id": robot["id"],
            "repo_id": "lerobot/test-policy",
            "fps": 10,
        },
    ).json()
    try:
        second = client.post(
            "/api/inference",
            json={
                "robot_id": robot["id"],
                "repo_id": "lerobot/test-policy",
                "fps": 10,
            },
        )
        assert second.status_code == 409
    finally:
        client.post(f"/api/inference/{first['id']}/stop", json={})


def test_stop_unknown_returns_404(client: TestClient) -> None:
    resp = client.post(f"/api/inference/{uuid4()}/stop", json={})
    assert resp.status_code == 404


def test_set_command_updates_task_description(client: TestClient) -> None:
    robot = _connected_robot(client)
    session = client.post(
        "/api/inference",
        json={
            "robot_id": robot["id"],
            "repo_id": "lerobot/test-policy",
            "fps": 10,
            "task_description": "initial",
        },
    ).json()
    try:
        updated = client.post(
            f"/api/inference/{session['id']}/command",
            json={"text": "revised"},
        )
        assert updated.status_code == 200
        assert updated.json()["task_description"] == "revised"
    finally:
        client.post(f"/api/inference/{session['id']}/stop", json={})


def test_deadman_toggle_is_reflected_on_session(client: TestClient) -> None:
    robot = _connected_robot(client)
    session = client.post(
        "/api/inference",
        json={
            "robot_id": robot["id"],
            "repo_id": "lerobot/test-policy",
            "fps": 10,
            "deadman_required": True,
        },
    ).json()
    try:
        held = client.post(f"/api/inference/{session['id']}/deadman", json={"held": True}).json()
        assert held["deadman_held"] is True

        released = client.post(f"/api/inference/{session['id']}/deadman", json={"held": False}).json()
        assert released["deadman_held"] is False
    finally:
        client.post(f"/api/inference/{session['id']}/stop", json={})


def test_ws_inference_emits_terminal_event_for_finished_session(
    client: TestClient,
) -> None:
    robot = _connected_robot(client)
    session = client.post(
        "/api/inference",
        json={
            "robot_id": robot["id"],
            "repo_id": "lerobot/test-policy",
            "fps": 10,
        },
    ).json()
    client.post(f"/api/inference/{session['id']}/stop", json={})
    with client.websocket_connect(f"/ws/inference/{session['id']}") as ws:
        first = ws.receive_json()
        assert first["type"] in {"stopped", "error"}
