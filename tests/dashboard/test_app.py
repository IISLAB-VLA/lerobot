"""Smoke tests for the dashboard FastAPI app factory."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.app import create_app  # noqa: E402
from lerobot.dashboard.core.config import DashboardConfig  # noqa: E402


@pytest.fixture
def app_client(tmp_path: Path) -> TestClient:
    config = DashboardConfig(storage_dir=tmp_path / "storage", static_dir=None)
    return TestClient(create_app(config))


def test_health_returns_ok(app_client: TestClient) -> None:
    response = app_client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "python" in body
    assert body["uptime_seconds"] >= 0.0


def test_cors_preflight_allowed(app_client: TestClient) -> None:
    response = app_client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_events_websocket_heartbeat(app_client: TestClient) -> None:
    with app_client.websocket_connect("/ws/events") as ws:
        msg = ws.receive_json()
        assert msg == {"type": "heartbeat"}
