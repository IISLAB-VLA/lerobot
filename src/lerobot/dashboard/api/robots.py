"""CRUD + lifecycle endpoints for :class:`RobotEntry`."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.api._deps import (
    get_registry,
    get_robot_manager,
    map_registry_error,
)
from lerobot.dashboard.services.registry import Registry, RegistryError
from lerobot.dashboard.services.registry_models import (
    Connection,
    RobotEntry,
    RobotStatus,
)
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/robots", tags=["robots"])


class RobotCreateRequest(BaseModel):
    """Shape accepted by ``POST /api/robots``.

    Intentionally omits ``id`` so that clients cannot pin UUIDs; the
    registry generates them. Everything else mirrors :class:`RobotEntry`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    robot_type: str
    connection: Connection
    cameras: list[UUID] = Field(default_factory=list)
    teleop: UUID | None = None
    image_ref: str | None = None


@router.get("", response_model=list[RobotEntry])
async def list_robots(registry: Registry = Depends(get_registry)) -> list[RobotEntry]:
    return await registry.list_robots()


@router.post("", response_model=RobotEntry, status_code=status.HTTP_201_CREATED)
async def create_robot(
    payload: RobotCreateRequest,
    registry: Registry = Depends(get_registry),
) -> RobotEntry:
    entry = RobotEntry(**payload.model_dump())
    try:
        return await registry.create_robot(entry)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.get("/{robot_id}", response_model=RobotEntry)
async def get_robot(
    robot_id: UUID, registry: Registry = Depends(get_registry)
) -> RobotEntry:
    try:
        return await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.patch("/{robot_id}", response_model=RobotEntry)
async def update_robot(
    robot_id: UUID,
    patch: dict = Body(...),
    registry: Registry = Depends(get_registry),
) -> RobotEntry:
    try:
        return await registry.update_robot(robot_id, patch)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.delete("/{robot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_robot(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> None:
    # Best-effort disconnect so we don't orphan live handles.
    if await manager.is_connected(robot_id):
        await manager.disconnect(robot_id)
    try:
        await registry.delete_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


# ---------------------------------------------------------------------------
# Lifecycle — connect / disconnect / status
# ---------------------------------------------------------------------------


@router.post("/{robot_id}/connect", response_model=RobotStatus)
async def connect_robot(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> RobotStatus:
    try:
        entry = await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    all_cameras = await registry.list_cameras()
    cam_map = {c.id: c for c in all_cameras}
    cameras = [cam_map[cid] for cid in entry.cameras if cid in cam_map]
    return await manager.connect(entry, cameras)


@router.post("/{robot_id}/disconnect", response_model=RobotStatus)
async def disconnect_robot(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> RobotStatus:
    try:
        await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return await manager.disconnect(robot_id)


@router.get("/{robot_id}/status", response_model=RobotStatus)
async def get_robot_status(
    robot_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: RobotManagerProtocol = Depends(get_robot_manager),
) -> RobotStatus:
    try:
        await registry.get_robot(robot_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return await manager.get_status(robot_id)
