"""HTTP-level tests for /api/robots, /api/cameras, /api/teleops."""

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
    config = DashboardConfig(storage_dir=tmp_path / "storage", static_dir=None)
    return TestClient(create_app(config))


def _camera_payload(name: str = "front") -> dict:
    return {
        "name": name,
        "backend": "opencv",
        "source": {"index": 0},
        "width": 640,
        "height": 480,
        "fps": 30,
    }


def _teleop_payload(name: str = "kb") -> dict:
    return {"name": name, "kind": "keyboard", "config": {"layout": "qwerty"}}


def _robot_payload(name: str = "so101", cameras: list | None = None, teleop=None) -> dict:
    payload = {
        "name": name,
        "robot_type": "so101_follower",
        "connection": {
            "kind": "serial",
            "port": "/dev/ttyUSB0",
            "baudrate": 1_000_000,
        },
        "cameras": cameras or [],
    }
    if teleop is not None:
        payload["teleop"] = teleop
    return payload


def test_list_empty(client: TestClient) -> None:
    assert client.get("/api/robots").json() == []
    assert client.get("/api/cameras").json() == []
    assert client.get("/api/teleops").json() == []


def test_robot_crud_round_trip(client: TestClient) -> None:
    camera = client.post("/api/cameras", json=_camera_payload()).json()
    teleop = client.post("/api/teleops", json=_teleop_payload()).json()

    create = client.post(
        "/api/robots",
        json=_robot_payload(cameras=[camera["id"]], teleop=teleop["id"]),
    )
    assert create.status_code == 201, create.text
    robot = create.json()
    assert robot["name"] == "so101"
    assert robot["cameras"] == [camera["id"]]
    assert robot["teleop"] == teleop["id"]

    fetched = client.get(f"/api/robots/{robot['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == robot["id"]

    patched = client.patch(f"/api/robots/{robot['id']}", json={"name": "renamed"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "renamed"

    deleted = client.delete(f"/api/robots/{robot['id']}")
    assert deleted.status_code == 204

    assert client.get(f"/api/robots/{robot['id']}").status_code == 404


def test_robot_network_connection(client: TestClient) -> None:
    payload = {
        "name": "ur5",
        "robot_type": "ur",
        "connection": {
            "kind": "network",
            "protocol": "rtde",
            "host": "192.168.1.10",
            "port": 30004,
        },
    }
    resp = client.post("/api/robots", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["connection"]["kind"] == "network"
    assert body["connection"]["protocol"] == "rtde"


def test_create_robot_unknown_camera(client: TestClient) -> None:
    resp = client.post("/api/robots", json=_robot_payload(cameras=[str(uuid4())]))
    assert resp.status_code == 422
    assert "unknown camera" in resp.json()["detail"].lower()


def test_delete_camera_blocked_while_referenced(client: TestClient) -> None:
    camera = client.post("/api/cameras", json=_camera_payload()).json()
    client.post("/api/robots", json=_robot_payload(cameras=[camera["id"]]))
    resp = client.delete(f"/api/cameras/{camera['id']}")
    assert resp.status_code == 422


def test_camera_patch_invalid_fails(client: TestClient) -> None:
    camera = client.post("/api/cameras", json=_camera_payload()).json()
    resp = client.patch(f"/api/cameras/{camera['id']}", json={"fps": -5})
    assert resp.status_code == 422


def test_teleop_crud(client: TestClient) -> None:
    create = client.post("/api/teleops", json=_teleop_payload())
    assert create.status_code == 201
    teleop = create.json()

    patched = client.patch(f"/api/teleops/{teleop['id']}", json={"name": "gamepad-1", "kind": "gamepad"})
    assert patched.status_code == 200
    assert patched.json()["kind"] == "gamepad"

    assert client.delete(f"/api/teleops/{teleop['id']}").status_code == 204
    assert client.get(f"/api/teleops/{teleop['id']}").status_code == 404


def test_robot_lifecycle_connect_status_disconnect(client: TestClient) -> None:
    robot = client.post("/api/robots", json=_robot_payload()).json()
    rid = robot["id"]

    status = client.get(f"/api/robots/{rid}/status").json()
    assert status["online"] is False

    connect = client.post(f"/api/robots/{rid}/connect").json()
    assert connect["online"] is True
    assert connect["connected_at"] is not None

    status = client.get(f"/api/robots/{rid}/status").json()
    assert status["online"] is True

    disconnect = client.post(f"/api/robots/{rid}/disconnect").json()
    assert disconnect["online"] is False


def test_lifecycle_404_for_unknown_robot(client: TestClient) -> None:
    ghost = uuid4()
    assert client.post(f"/api/robots/{ghost}/connect").status_code == 404
    assert client.post(f"/api/robots/{ghost}/disconnect").status_code == 404
    assert client.get(f"/api/robots/{ghost}/status").status_code == 404


def test_delete_robot_also_disconnects(client: TestClient) -> None:
    robot = client.post("/api/robots", json=_robot_payload()).json()
    client.post(f"/api/robots/{robot['id']}/connect")
    resp = client.delete(f"/api/robots/{robot['id']}")
    assert resp.status_code == 204
    # After deletion the robot is gone — status must 404.
    assert client.get(f"/api/robots/{robot['id']}/status").status_code == 404
