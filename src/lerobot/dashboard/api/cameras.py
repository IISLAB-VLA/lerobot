"""CRUD endpoints for :class:`CameraEntry`."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.api._deps import get_registry, map_registry_error
from lerobot.dashboard.services.registry import Registry, RegistryError
from lerobot.dashboard.services.registry_models import (
    CameraBackend,
    CameraEntry,
    CameraSource,
)

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
async def get_camera(
    camera_id: UUID, registry: Registry = Depends(get_registry)
) -> CameraEntry:
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
    camera_id: UUID, registry: Registry = Depends(get_registry)
) -> None:
    try:
        await registry.delete_camera(camera_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
