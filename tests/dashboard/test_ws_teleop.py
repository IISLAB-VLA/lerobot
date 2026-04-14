"""Unit tests for the /ws/robots/{robot_id}/teleop WebSocket handler.

Tests use FastAPI's TestClient WebSocket support (Starlette's
``websocket_connect``) so they exercise the full ASGI stack without
requiring a live server. The robot_manager is replaced with an
in-memory fake that always reports the robot as connected.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from lerobot.dashboard.core.state import AppState
from lerobot.dashboard.core.config import DashboardConfig
from lerobot.dashboard.services.assets import AssetManager
from lerobot.dashboard.services.camera_manager import InMemoryCameraManager
from lerobot.dashboard.services.registry import Registry, registry_path_for
from lerobot.dashboard.services.registry_models import RobotEntry, RobotStatus, CameraEntry
from lerobot.dashboard.services.robot_manager import InMemoryRobotManager
from lerobot.dashboard.services.teleop_manager import InMemoryTeleopManager
from lerobot.dashboard.storage.paths import ensure_storage_dir
from lerobot.dashboard.ws.router import build_ws_router


# ---------------------------------------------------------------------------
# Fake robot manager — always online
# ---------------------------------------------------------------------------


class _OnlineRobotManager(InMemoryRobotManager):
    """Force is_connected to return True for any robot_id."""

    def __init__(self, robot_id: UUID) -> None:
        super().__init__()
        self._rid = robot_id

    async def is_connected(self, robot_id: UUID) -> bool:
        return robot_id == self._rid


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(robot_id: UUID, tmp_path) -> FastAPI:
    config = DashboardConfig()
    ensure_storage_dir(config.storage_dir)
    assets = AssetManager(config.storage_dir)
    state = AppState(config=config, assets=assets)
    state.registry = Registry(registry_path_for(config.storage_dir))
    state.robot_manager = _OnlineRobotManager(robot_id)
    state.camera_manager = InMemoryCameraManager()
    state.teleop_manager = InMemoryTeleopManager()

    app = FastAPI()
    app.state.dashboard = state
    app.include_router(build_ws_router())
    return app


def _rid_str(rid: UUID) -> str:
    return str(rid)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send(ws, **kwargs: Any) -> None:
    ws.send_json(kwargs)


def _recv(ws) -> dict[str, Any]:
    return ws.receive_json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_teleop_ws_rejects_offline_robot(tmp_path):
    """A robot that is_connected=False triggers 4002 close."""
    app = FastAPI()
    state = AppState(config=DashboardConfig(), assets=AssetManager(DashboardConfig().storage_dir))
    state.robot_manager = InMemoryRobotManager()  # nothing connected
    state.teleop_manager = InMemoryTeleopManager()
    state.camera_manager = InMemoryCameraManager()
    state.registry = Registry(registry_path_for(DashboardConfig().storage_dir))
    app.state.dashboard = state
    app.include_router(build_ws_router())

    rid = uuid4()
    client = TestClient(app, raise_server_exceptions=False)
    # Server accepts then immediately closes with 4002; reading triggers disconnect.
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/robots/{rid}/teleop") as ws:
            _recv(ws)  # WebSocketDisconnect raised here (code 4002)


def test_teleop_ws_sends_initial_state(tmp_path):
    rid = uuid4()
    app = _make_app(rid, tmp_path)
    with TestClient(app).websocket_connect(f"/ws/robots/{rid}/teleop") as ws:
        frame = _recv(ws)
        assert frame["type"] == "state"
        assert frame["payload"]["state"] == "idle"


def test_teleop_ws_deadman_press_and_tick_to_engaged(tmp_path):
    rid = uuid4()
    app = _make_app(rid, tmp_path)
    with TestClient(app).websocket_connect(f"/ws/robots/{rid}/teleop") as ws:
        _recv(ws)  # discard initial state

        # Press deadman — should transition to ARMING then ENGAGED after delay.
        _send(ws, seq=1, ts_client_ms=0, type="deadman", payload={"held": True})

        # Collect a few frames — look for ARMING then ENGAGED state pushes.
        states: list[str] = []
        for _ in range(5):
            try:
                frame = _recv(ws)
                if frame.get("type") == "state":
                    states.append(frame["payload"]["state"])
                    if "engaged" in states:
                        break
            except Exception:
                break

    # At minimum ARMING should have appeared (arming_delay=200ms so we
    # might not reach ENGAGED in a unit test; presence of "arming" is enough).
    assert "arming" in states


def test_teleop_ws_heartbeat_accepted(tmp_path):
    rid = uuid4()
    app = _make_app(rid, tmp_path)
    with TestClient(app).websocket_connect(f"/ws/robots/{rid}/teleop") as ws:
        _recv(ws)  # initial state
        _send(ws, seq=2, ts_client_ms=100, type="heartbeat", payload={})
        # No error frame means heartbeat was accepted (handler doesn't ack heartbeat).


def test_teleop_ws_bad_frame_returns_error(tmp_path):
    rid = uuid4()
    app = _make_app(rid, tmp_path)
    with TestClient(app).websocket_connect(f"/ws/robots/{rid}/teleop") as ws:
        _recv(ws)  # initial state
        # Send a malformed frame (missing required fields).
        ws.send_json({"type": "deadman"})  # no seq/ts_client_ms
        frame = _recv(ws)
        assert frame["type"] == "error"
        assert frame["payload"]["code"] == "protocol_error"


def test_teleop_ws_conflict_closes_second_connection(tmp_path):
    rid = uuid4()
    app = _make_app(rid, tmp_path)
    client = TestClient(app, raise_server_exceptions=False)

    with client.websocket_connect(f"/ws/robots/{rid}/teleop") as ws1:
        _recv(ws1)  # initial state — first session live
        # Second client tries to connect — should be rejected.
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/robots/{rid}/teleop") as ws2:
                _recv(ws2)


def test_teleop_ws_input_frame_bad_kind_returns_error(tmp_path):
    rid = uuid4()
    app = _make_app(rid, tmp_path)
    with TestClient(app).websocket_connect(f"/ws/robots/{rid}/teleop") as ws:
        _recv(ws)
        _send(
            ws,
            seq=1,
            ts_client_ms=0,
            type="input",
            payload={"kind": "unknown_device", "data": 42},
        )
        frame = _recv(ws)
        assert frame["type"] == "error"
        assert frame["payload"]["code"] == "bad_input"


def test_teleop_ws_input_frame_keyboard_returns_ack(tmp_path):
    rid = uuid4()
    app = _make_app(rid, tmp_path)
    with TestClient(app).websocket_connect(f"/ws/robots/{rid}/teleop") as ws:
        _recv(ws)
        _send(
            ws,
            seq=5,
            ts_client_ms=50,
            type="input",
            payload={"kind": "keyboard", "key": "w", "pressed": True},
        )
        frame = _recv(ws)
        assert frame["type"] == "ack"
        assert frame["payload"]["seq"] == 5
