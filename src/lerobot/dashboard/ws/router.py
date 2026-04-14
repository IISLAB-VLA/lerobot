"""WebSocket endpoints.

The dashboard uses two long-lived channels:

* ``/ws/events`` — server-to-client push (device status, recording progress,
  backend logs). Implemented here as a minimal echo/heartbeat loop;
  richer event fan-out is added incrementally.
* ``/ws/teleop`` — bi-directional teleop command stream. Implemented in
  task #11 by streaming-engineer; this module only reserves the route.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from lerobot.dashboard.services.calibration import SessionNotFoundError

logger = logging.getLogger(__name__)


def build_ws_router() -> APIRouter:
    router = APIRouter(prefix="/ws")

    @router.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json({"type": "heartbeat"})
                await asyncio.sleep(15)
        except WebSocketDisconnect:
            logger.debug("events ws disconnected")

    @router.websocket("/teleop")
    async def teleop(websocket: WebSocket) -> None:
        # Placeholder: streaming-engineer (task #11) implements the real protocol.
        await websocket.accept()
        await websocket.send_json({"type": "not_implemented", "task": 11})
        await websocket.close(code=1011)

    @router.websocket("/robots/{robot_id}/calibrate")
    async def calibrate(websocket: WebSocket, robot_id: UUID, session_id: str) -> None:
        controller = getattr(websocket.app.state.dashboard, "calibration", None)
        if controller is None:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        aiter = controller.subscribe(robot_id, session_id)

        async def _drain_client() -> None:
            # Heartbeats are the only legal C2S message; other frames are ignored.
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass

        drain = asyncio.create_task(_drain_client(), name=f"calibrate-ws-drain-{session_id}")
        try:
            async for event in aiter:
                try:
                    await websocket.send_json(event)
                except WebSocketDisconnect:
                    break
        except SessionNotFoundError as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
        finally:
            drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await websocket.close()
            except Exception:
                pass

    return router
