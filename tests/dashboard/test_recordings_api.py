"""HTTP + WS surface for /api/recordings (task #13 phase 1b)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.app import create_app  # noqa: E402
from lerobot.dashboard.core.config import DashboardConfig  # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = DashboardConfig(
        storage_dir=tmp_path / "storage",
        static_dir=None,
        fake_devices=True,
    )
    return TestClient(create_app(config))


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


def test_list_starts_empty(client: TestClient) -> None:
    assert client.get("/api/recordings").json() == []


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
        "/api/recordings",
        json={
            "robot_id": robot["id"],
            "dataset_name": "pilot",
            "task_description": "t",
            "fps": 10,
        },
    )
    assert resp.status_code == 422


def test_start_returns_202_and_lists_session(client: TestClient) -> None:
    robot = _connected_robot(client)
    resp = client.post(
        "/api/recordings",
        json={
            "robot_id": robot["id"],
            "dataset_name": "pilot_v1",
            "task_description": "pick the cube",
            "fps": 10,
        },
    )
    assert resp.status_code == 202, resp.text
    session = resp.json()
    assert session["status"] == "recording"
    assert session["dataset_name"] == "pilot_v1"
    try:
        listed = client.get("/api/recordings").json()
        assert [s["id"] for s in listed] == [session["id"]]

        fetched = client.get(f"/api/recordings/{session['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == session["id"]
    finally:
        client.post(
            f"/api/recordings/{session['id']}/stop",
            json={"save": False},
        )


def test_start_same_robot_twice_returns_409(client: TestClient) -> None:
    robot = _connected_robot(client)
    first = client.post(
        "/api/recordings",
        json={
            "robot_id": robot["id"],
            "dataset_name": "a",
            "task_description": "t",
            "fps": 10,
        },
    ).json()
    try:
        second = client.post(
            "/api/recordings",
            json={
                "robot_id": robot["id"],
                "dataset_name": "b",
                "task_description": "t",
                "fps": 10,
            },
        )
        assert second.status_code == 409
    finally:
        client.post(f"/api/recordings/{first['id']}/stop", json={"save": False})


def test_stop_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.post(f"/api/recordings/{uuid4()}/stop", json={"save": False})
    assert resp.status_code == 404


def test_stop_discard_flow(client: TestClient) -> None:
    robot = _connected_robot(client)
    session = client.post(
        "/api/recordings",
        json={
            "robot_id": robot["id"],
            "dataset_name": "discardable",
            "task_description": "t",
            "fps": 10,
        },
    ).json()
    resp = client.post(f"/api/recordings/{session['id']}/stop", json={"save": False})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "discarded"
    assert body["saved"] is False


def test_ws_recordings_emits_terminal_event_for_finished_session(
    client: TestClient,
) -> None:
    robot = _connected_robot(client)
    session = client.post(
        "/api/recordings",
        json={
            "robot_id": robot["id"],
            "dataset_name": "wsend",
            "task_description": "t",
            "fps": 10,
        },
    ).json()
    # Stop the session before subscribing — the late-subscriber path should
    # still surface a terminal event and then close the socket.
    client.post(f"/api/recordings/{session['id']}/stop", json={"save": False})
    with client.websocket_connect(f"/ws/recordings/{session['id']}") as ws:
        first = ws.receive_json()
        assert first["type"] in {"stopped", "error"}
