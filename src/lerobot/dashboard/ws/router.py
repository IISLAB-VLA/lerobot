"""WebSocket endpoints.

The dashboard uses these long-lived channels:

* ``/ws/events`` — server-to-client push (device status, recording progress,
  backend logs). Implemented here as a minimal echo/heartbeat loop;
  richer event fan-out is added incrementally.
* ``/ws/teleop`` — bi-directional teleop command stream. Implemented in
  task #11 by streaming-engineer; this module only reserves the route.
* ``/ws/recordings/{session_id}`` — server-to-client push of recording
  progress events (task #13). Wraps :meth:`RecorderService.subscribe`.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from lerobot.dashboard.services.recorder import (
    RecorderNotFoundError,
    RecorderService,
)

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

    @router.websocket("/recordings/{session_id}")
    async def recordings(websocket: WebSocket, session_id: UUID) -> None:
        """Forward :class:`ProgressEvent` payloads to a browser subscriber."""
        recorder: RecorderService | None = getattr(websocket.app.state.dashboard, "recorder", None)
        if recorder is None:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return
        await websocket.accept()
        try:
            async for event in recorder.subscribe(session_id):
                await websocket.send_json(event.model_dump(mode="json"))
        except RecorderNotFoundError:
            await websocket.send_json({"type": "error", "message": "session not found"})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except WebSocketDisconnect:
            logger.debug("recordings ws disconnected (%s)", session_id)

    return router
