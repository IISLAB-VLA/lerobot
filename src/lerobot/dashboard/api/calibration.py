"""Calibration REST endpoints.

Contract v3 (confirmed with frontend-architect-3 on 2026-04-14):

* ``POST /api/robots/{robot_id}/calibrate/start``  — create a session.
* ``POST /api/robots/{robot_id}/calibrate/ack``    — advance the current step.
* ``POST /api/robots/{robot_id}/calibrate/cancel`` — tear down the session.
* ``GET  /api/robots/{robot_id}/calibrate/status`` — resume snapshot.
* ``GET  /api/robots/{robot_id}/calibration``      — last completed summary.

User actions only flow through REST; the WS channel in
:mod:`lerobot.dashboard.ws.router` is a read-only event stream.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from lerobot.dashboard.api._deps import (
    get_registry,
    get_robot_manager,
    map_registry_error,
)
from lerobot.dashboard.services.calibration import (
    CalibrationAckRequest,
    CalibrationCancelRequest,
    CalibrationController,
    CalibrationStartResponse,
    CalibrationStatusResponse,
    CalibrationSummary,
    RobotNotConnectedError,
    SessionAlreadyActiveError,
    SessionMismatchError,
    SessionNotFoundError,
)
from lerobot.dashboard.services.registry import Registry, RegistryError
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol

router = APIRouter(prefix="/robots/{robot_id}", tags=["calibration"])


class CalibrationConflict(BaseModel):
    """Response body for 409 conflicts (FE reads ``existing_session_id``/``current_step_id``)."""

    model_config = ConfigDict(extra="forbid")

    detail: str
    existing_session_id: str | None = None
    current_step_id: str | None = None


class AckAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: Literal[True] = True


class CancelAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancelled: Literal[True] = True


def get_calibration(request: Request) -> CalibrationController:
    controller = getattr(request.app.state.dashboard, "calibration", None)
    if controller is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="calibration controller is not initialized",
        )
    return controller


async def _resolve_robot_type(registry: Registry, robot_id: UUID) -> str:
    try:
        robot = await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return robot.robot_type


@router.post(
    "/calibrate/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CalibrationStartResponse,
    responses={409: {"model": CalibrationConflict}, 404: {"model": CalibrationConflict}},
)
async def start_calibration(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    robot_manager: RobotManagerProtocol = Depends(get_robot_manager),
    controller: CalibrationController = Depends(get_calibration),
) -> CalibrationStartResponse:
    robot_type = await _resolve_robot_type(registry, robot_id)
    try:
        return await controller.start(robot_id, robot_type, robot_manager)
    except RobotNotConnectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": str(exc)},
        ) from exc
    except SessionAlreadyActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": str(exc), "existing_session_id": exc.existing_session_id},
        ) from exc


@router.post(
    "/calibrate/ack",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AckAccepted,
    responses={409: {"model": CalibrationConflict}},
)
async def ack_calibration(
    robot_id: UUID,
    body: CalibrationAckRequest,
    controller: CalibrationController = Depends(get_calibration),
) -> AckAccepted:
    try:
        await controller.ack(robot_id, body.session_id, body.step_id)
    except SessionMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"detail": str(exc), "current_step_id": exc.current_step_id},
        ) from exc
    return AckAccepted()


@router.post(
    "/calibrate/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CancelAccepted,
    responses={404: {"model": CalibrationConflict}},
)
async def cancel_calibration(
    robot_id: UUID,
    body: CalibrationCancelRequest,
    controller: CalibrationController = Depends(get_calibration),
) -> CancelAccepted:
    try:
        await controller.cancel(robot_id, body.session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": str(exc)},
        ) from exc
    return CancelAccepted()


@router.get(
    "/calibrate/status",
    response_model=CalibrationStatusResponse,
    responses={404: {"model": CalibrationConflict}},
)
async def calibrate_status(
    robot_id: UUID,
    controller: CalibrationController = Depends(get_calibration),
) -> CalibrationStatusResponse:
    snapshot = await controller.get_status(robot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"no active calibration session for robot {robot_id}"},
        )
    return snapshot


@router.get(
    "/calibration",
    response_model=CalibrationSummary,
    responses={404: {"model": CalibrationConflict}},
)
async def latest_calibration(
    robot_id: UUID,
    controller: CalibrationController = Depends(get_calibration),
) -> CalibrationSummary:
    summary = await controller.latest(robot_id)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"detail": f"no calibration summary for robot {robot_id}"},
        )
    return summary
