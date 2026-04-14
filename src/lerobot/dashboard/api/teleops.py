"""CRUD endpoints for :class:`TeleopEntry`."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.api._deps import get_registry, map_registry_error
from lerobot.dashboard.services.registry import Registry, RegistryError
from lerobot.dashboard.services.registry_models import TeleopEntry, TeleopKind

router = APIRouter(prefix="/teleops", tags=["teleops"])


class TeleopCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    kind: TeleopKind
    config: dict[str, object] = Field(default_factory=dict)


@router.get("", response_model=list[TeleopEntry])
async def list_teleops(registry: Registry = Depends(get_registry)) -> list[TeleopEntry]:
    return await registry.list_teleops()


@router.post("", response_model=TeleopEntry, status_code=status.HTTP_201_CREATED)
async def create_teleop(
    payload: TeleopCreateRequest,
    registry: Registry = Depends(get_registry),
) -> TeleopEntry:
    entry = TeleopEntry(**payload.model_dump())
    try:
        return await registry.create_teleop(entry)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.get("/{teleop_id}", response_model=TeleopEntry)
async def get_teleop(teleop_id: UUID, registry: Registry = Depends(get_registry)) -> TeleopEntry:
    try:
        return await registry.get_teleop(teleop_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.patch("/{teleop_id}", response_model=TeleopEntry)
async def update_teleop(
    teleop_id: UUID,
    patch: dict = Body(...),
    registry: Registry = Depends(get_registry),
) -> TeleopEntry:
    try:
        return await registry.update_teleop(teleop_id, patch)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc


@router.delete("/{teleop_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_teleop(teleop_id: UUID, registry: Registry = Depends(get_registry)) -> None:
    try:
        await registry.delete_teleop(teleop_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
