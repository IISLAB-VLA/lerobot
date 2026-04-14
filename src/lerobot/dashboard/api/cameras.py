"""CRUD endpoints for :class:`CameraEntry`."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.api._deps import (
    get_camera_manager,
    get_registry,
    map_registry_error,
)
from lerobot.dashboard.services.camera_manager import CameraManagerProtocol
from lerobot.dashboard.services.registry import Registry, RegistryError
from lerobot.dashboard.services.registry_models import (
    CameraBackend,
    CameraEntry,
    CameraSource,
    CameraStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cameras", tags=["cameras"])


class CameraCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    backend: CameraBackend
    source: CameraSource
    width: int = Field(..., ge=16, le=7680)
    height: int = Field(..., ge=16, le=4320)
    fps: float = Field(..., gt=0, le=240)
    codec_hint: str | None = None


@router.get("", response_model=list[CameraEntry])
async def list_cameras(registry: Registry = Depends(get_registry)) -> list[CameraEntry]:
    return await registry.list_cameras()


@router.post("", response_model=CameraEntry, status_code=status.HTTP_201_CREATED)
async def create_camera(
    payload: CameraCreateRequest,
    registry: Registry = Depends(get_registry),
) -> CameraEntry:
    entry = CameraEntry(**payload.model_dump())
    try:
        return await registry.create_camera(entry)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.get("/{camera_id}", response_model=CameraEntry)
async def get_camera(camera_id: UUID, registry: Registry = Depends(get_registry)) -> CameraEntry:
    try:
        return await registry.get_camera(camera_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.patch("/{camera_id}", response_model=CameraEntry)
async def update_camera(
    camera_id: UUID,
    patch: dict = Body(...),
    registry: Registry = Depends(get_registry),
) -> CameraEntry:
    try:
        return await registry.update_camera(camera_id, patch)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: CameraManagerProtocol = Depends(get_camera_manager),
) -> None:
    # Best-effort release so we don't leak a capture loop when the registry
    # entry disappears.
    try:
        if await manager.is_open(camera_id):
            await manager.close(camera_id)
    except Exception as exc:  # noqa: BLE001 — registry delete is still the source of truth
        logger.warning("camera %s close during delete failed: %s", camera_id, exc)
    try:
        await registry.delete_camera(camera_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


# ---------------------------------------------------------------------------
# Lifecycle — open / close / status. Mirrors the robot lifecycle endpoints
# so consumers (recorder, VLA inference, the streaming WebRTC track) can
# explicitly own the capture handle rather than relying on
# ``subscribe``-triggered implicit opens.
# ---------------------------------------------------------------------------


@router.post("/{camera_id}/open", response_model=CameraStatus)
async def open_camera(
    camera_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: CameraManagerProtocol = Depends(get_camera_manager),
) -> CameraStatus:
    try:
        entry = await registry.get_camera(camera_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return await manager.open(entry)


@router.post("/{camera_id}/close", response_model=CameraStatus)
async def close_camera(
    camera_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: CameraManagerProtocol = Depends(get_camera_manager),
) -> CameraStatus:
    try:
        await registry.get_camera(camera_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return await manager.close(camera_id)


@router.get("/{camera_id}/status", response_model=CameraStatus)
async def get_camera_status(
    camera_id: UUID,
    registry: Registry = Depends(get_registry),
    manager: CameraManagerProtocol = Depends(get_camera_manager),
) -> CameraStatus:
    try:
        await registry.get_camera(camera_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return await manager.get_status(camera_id)
