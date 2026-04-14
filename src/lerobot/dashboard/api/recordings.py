"""REST endpoints for dataset recording sessions (task #13 phase 1b)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.api._deps import get_recorder, map_recorder_error
from lerobot.dashboard.services.recorder import (
    RecorderError,
    RecorderService,
    RecordingSession,
    StartRequest,
)

router = APIRouter(prefix="/recordings", tags=["recordings"])


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    save: bool = Field(
        default=True,
        description="Finalize the dataset (True) or discard buffered frames (False).",
    )


@router.get("", response_model=list[RecordingSession])
async def list_recordings(
    recorder: RecorderService = Depends(get_recorder),
) -> list[RecordingSession]:
    return await recorder.list()


@router.post("", response_model=RecordingSession, status_code=status.HTTP_202_ACCEPTED)
async def start_recording(
    payload: StartRequest,
    recorder: RecorderService = Depends(get_recorder),
) -> RecordingSession:
    try:
        return await recorder.start(payload)
    except RecorderError as exc:
        raise map_recorder_error(exc) from exc


@router.get("/{session_id}", response_model=RecordingSession)
async def get_recording(
    session_id: UUID, recorder: RecorderService = Depends(get_recorder)
) -> RecordingSession:
    try:
        return await recorder.get(session_id)
    except RecorderError as exc:
        raise map_recorder_error(exc) from exc


@router.post(
    "/{session_id}/stop",
    response_model=RecordingSession,
    status_code=status.HTTP_202_ACCEPTED,
)
async def stop_recording(
    session_id: UUID,
    payload: StopRequest = Body(default_factory=StopRequest),
    recorder: RecorderService = Depends(get_recorder),
) -> RecordingSession:
    try:
        return await recorder.stop(session_id, save=payload.save)
    except RecorderError as exc:
        raise map_recorder_error(exc) from exc
