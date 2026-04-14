"""REST endpoints for VLA inference sessions (task #14 phase 1b)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.api._deps import (
    get_inference,
    map_inference_error,
)
from lerobot.dashboard.services.inference import (
    InferenceError,
    InferenceService,
    InferenceSession,
    StartRequest,
)

router = APIRouter(prefix="/inference", tags=["inference"])


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=0, max_length=500)


class DeadmanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    held: bool


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="client", max_length=80)


@router.get("", response_model=list[InferenceSession])
async def list_inference_sessions(
    inference: InferenceService = Depends(get_inference),
) -> list[InferenceSession]:
    return await inference.list()


@router.post(
    "",
    response_model=InferenceSession,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_inference(
    payload: StartRequest,
    inference: InferenceService = Depends(get_inference),
) -> InferenceSession:
    try:
        return await inference.start(payload)
    except InferenceError as exc:
        raise map_inference_error(exc) from exc


@router.get("/{session_id}", response_model=InferenceSession)
async def get_inference_session(
    session_id: UUID,
    inference: InferenceService = Depends(get_inference),
) -> InferenceSession:
    try:
        return await inference.get(session_id)
    except InferenceError as exc:
        raise map_inference_error(exc) from exc


@router.post(
    "/{session_id}/stop",
    response_model=InferenceSession,
    status_code=status.HTTP_202_ACCEPTED,
)
async def stop_inference(
    session_id: UUID,
    payload: StopRequest = StopRequest(),
    inference: InferenceService = Depends(get_inference),
) -> InferenceSession:
    try:
        return await inference.stop(session_id, reason=payload.reason)
    except InferenceError as exc:
        raise map_inference_error(exc) from exc


@router.post("/{session_id}/command", response_model=InferenceSession)
async def set_inference_command(
    session_id: UUID,
    payload: CommandRequest,
    inference: InferenceService = Depends(get_inference),
) -> InferenceSession:
    try:
        return await inference.set_command(session_id, payload.text)
    except InferenceError as exc:
        raise map_inference_error(exc) from exc


@router.post("/{session_id}/deadman", response_model=InferenceSession)
async def set_inference_deadman(
    session_id: UUID,
    payload: DeadmanRequest,
    inference: InferenceService = Depends(get_inference),
) -> InferenceSession:
    try:
        return await inference.set_deadman(session_id, payload.held)
    except InferenceError as exc:
        raise map_inference_error(exc) from exc
