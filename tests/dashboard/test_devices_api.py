"""Device discovery REST API tests.

Focuses on the fake-device code path (so CI has no hardware
dependency) and on the contract used by the frontend Add-Robot modal +
qa-engineer's Playwright specs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.api import devices as devices_module  # noqa: E402
from lerobot.dashboard.app import create_app  # noqa: E402
from lerobot.dashboard.core.config import DashboardConfig  # noqa: E402


@pytest.fixture
def fake_client(tmp_path: Path) -> TestClient:
    config = DashboardConfig(storage_dir=tmp_path / "storage", fake_devices=True)
    return TestClient(create_app(config))


@pytest.fixture
def real_client(tmp_path: Path) -> TestClient:
    config = DashboardConfig(storage_dir=tmp_path / "storage", fake_devices=False)
    return TestClient(create_app(config))


def test_fake_serial_returns_two_stable_paths(fake_client: TestClient) -> None:
    body = fake_client.get("/api/devices/serial").json()
    assert len(body["ports"]) == 2
    ports = {p["raw_port"]: p for p in body["ports"]}
    assert ports["/dev/ttyUSB0"]["port"].startswith("/dev/serial/by-id/")
    assert ports["/dev/ttyACM0"]["vid"] == "0x1a86"
    assert ports["/dev/ttyACM0"]["pid"] == "0x7523"
    for p in body["ports"]:
        assert set(p.keys()) >= {
            "port",
            "raw_port",
            "description",
            "vid",
            "pid",
            "serial_number",
        }


def test_fake_cameras_payload_includes_realsense(fake_client: TestClient) -> None:
    body = fake_client.get("/api/devices/cameras").json()
    assert body["realsense_available"] is True
    backends = {c["backend"] for c in body["cameras"]}
    assert backends == {"opencv", "realsense"}
    opencv_cams = [c for c in body["cameras"] if c["backend"] == "opencv"]
    assert opencv_cams[0]["default_profile"]["width"] == 1280
    assert opencv_cams[0]["path"] == "/dev/video0"
    assert opencv_cams[0]["index"] == 0


def test_fake_network_probe_reachable_localhost(fake_client: TestClient) -> None:
    resp = fake_client.post(
        "/api/devices/network", json={"host": "127.0.0.1", "protocol": "rtde"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol"] == "rtde"
    assert body["port"] == 30004
    assert body["reachable"] is True
    assert body["latency_ms"] is not None


def test_fake_network_probe_unreachable_external_host(fake_client: TestClient) -> None:
    body = fake_client.post(
        "/api/devices/network", json={"host": "192.0.2.1", "protocol": "fci"}
    ).json()
    assert body["reachable"] is False
    assert body["port"] == 10001
    assert body["protocol"] == "fci"


def test_network_probe_requires_protocol_or_port(fake_client: TestClient) -> None:
    resp = fake_client.post("/api/devices/network", json={"host": "127.0.0.1"})
    assert resp.status_code == 422


def test_network_probe_unknown_port_defaults_to_tcp(fake_client: TestClient) -> None:
    body = fake_client.post(
        "/api/devices/network", json={"host": "127.0.0.1", "port": 12345}
    ).json()
    assert body["protocol"] == "tcp"
    assert body["port"] == 12345


def test_network_probe_detects_rtde_from_default_port(fake_client: TestClient) -> None:
    body = fake_client.post(
        "/api/devices/network", json={"host": "127.0.0.1", "port": 30004}
    ).json()
    assert body["protocol"] == "rtde"


def test_real_serial_calls_pyserial(real_client: TestClient) -> None:
    sentinel = [
        devices_module.SerialPortInfo(
            port="/dev/ttyACM9",
            raw_port="/dev/ttyACM9",
            description="Sentinel",
            vid="0x1234",
            pid="0x5678",
            serial_number="SN-XYZ",
        )
    ]
    with patch.object(devices_module, "_list_serial_ports_sync", return_value=sentinel):
        body = real_client.get("/api/devices/serial").json()
    assert body["ports"][0]["serial_number"] == "SN-XYZ"


def test_real_cameras_tolerates_missing_realsense(real_client: TestClient) -> None:
    with (
        patch.object(devices_module, "_list_opencv_cameras_sync", return_value=[]),
        patch.object(devices_module, "_list_realsense_cameras_sync", return_value=([], False)),
    ):
        body = real_client.get("/api/devices/cameras").json()
    assert body["cameras"] == []
    assert body["realsense_available"] is False


def test_network_probe_times_out_on_unroutable_host(real_client: TestClient) -> None:
    resp = real_client.post(
        "/api/devices/network",
        json={"host": "192.0.2.1", "port": 30004, "timeout_ms": 100},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False
    assert body["error"] is not None


def test_stable_serial_path_falls_back_to_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices_module.platform, "system", lambda: "Darwin")
    assert devices_module._stable_serial_path("/dev/tty.usbmodem1") == "/dev/tty.usbmodem1"
