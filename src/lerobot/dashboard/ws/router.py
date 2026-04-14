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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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

    return router
