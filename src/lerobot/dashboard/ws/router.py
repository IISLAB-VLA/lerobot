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
from contextlib import suppress
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from starlette.websockets import WebSocketState

from lerobot.dashboard.services.benchmark import RunNotFoundError
from lerobot.dashboard.services.calibration import SessionNotFoundError
from lerobot.dashboard.services.inference import (
    InferenceNotFoundError,
    InferenceService,
)
from lerobot.dashboard.services.recorder import (
    RecorderNotFoundError,
    RecorderService,
)

logger = logging.getLogger(__name__)


async def _safe_send_json(websocket: WebSocket, payload: dict) -> None:
    """Send a JSON payload only while the socket is still open.

    Starlette emits an ASGI warning when a handler does
    ``websocket.send_*`` after a prior ``websocket.close`` — the terminal
    error branches below are exactly those races (client unmounted while
    the server was about to emit a final 'error' frame). Gating on
    ``application_state`` keeps the log clean without swallowing real bugs.
    """
    if websocket.application_state != WebSocketState.CONNECTED:
        return
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.send_json(payload)


async def _safe_close(websocket: WebSocket, code: int = 1000) -> None:
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code)


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
            await _safe_send_json(websocket, {"type": "error", "message": str(exc)})
        finally:
            drain.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await drain
            await _safe_close(websocket)

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
            await _safe_send_json(websocket, {"type": "error", "message": "session not found"})
            await _safe_close(websocket, code=status.WS_1008_POLICY_VIOLATION)
        except WebSocketDisconnect:
            logger.debug("recordings ws disconnected (%s)", session_id)

    @router.websocket("/benchmarks/{run_id}")
    async def benchmarks_run(websocket: WebSocket, run_id: str) -> None:
        controller = getattr(websocket.app.state.dashboard, "benchmark", None)
        if controller is None:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        aiter = controller.subscribe(run_id)

        async def _drain_client() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass

        drain = asyncio.create_task(_drain_client(), name=f"benchmark-ws-drain-{run_id}")
        try:
            async for event in aiter:
                try:
                    await websocket.send_json(event)
                except WebSocketDisconnect:
                    break
        except RunNotFoundError as exc:
            await _safe_send_json(websocket, {"type": "error", "message": str(exc)})
        finally:
            drain.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await drain
            await _safe_close(websocket)

    @router.websocket("/inference/{session_id}")
    async def inference(websocket: WebSocket, session_id: UUID) -> None:
        """Forward :class:`StepEvent` payloads to a browser subscriber."""
        service: InferenceService | None = getattr(websocket.app.state.dashboard, "inference", None)
        if service is None:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return
        await websocket.accept()
        try:
            async for event in service.subscribe(session_id):
                await websocket.send_json(event.model_dump(mode="json"))
        except InferenceNotFoundError:
            await _safe_send_json(websocket, {"type": "error", "message": "session not found"})
            await _safe_close(websocket, code=status.WS_1008_POLICY_VIOLATION)
        except WebSocketDisconnect:
            logger.debug("inference ws disconnected (%s)", session_id)

    return router
