"""CRUD endpoints for :class:`CameraEntry`."""

from __future__ import annotations

import logging
from typing import Literal
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

# ---------------------------------------------------------------------------
# Camera capabilities schema (used by Task #22 settings drawer)
# ---------------------------------------------------------------------------

CameraSourceType = Literal["v4l2", "realsense", "fake"]

# Standard resolutions offered in the picker regardless of backend.
_STANDARD_RESOLUTIONS: list[dict[str, int]] = [
    {"width": 320, "height": 240},
    {"width": 640, "height": 480},
    {"width": 848, "height": 480},
    {"width": 1280, "height": 720},
    {"width": 1920, "height": 1080},
]

_STANDARD_FPS: list[float] = [5, 10, 15, 25, 30, 60, 90]

# Ordered by server preference (best transport codec first)
_CODECS_V4L2: list[str] = ["h264", "vp9", "vp8"]
_CODECS_REALSENSE: list[str] = ["h264", "vp8"]
_CODECS_FAKE: list[str] = ["vp8", "vp9"]


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int
    height: int


class CurrentMode(BaseModel):
    """Active capture parameters as configured in the registry entry."""

    model_config = ConfigDict(extra="forbid")
    width: int
    height: int
    fps: float
    codec: str | None


class CameraCapabilities(BaseModel):
    """Advertised capabilities for the settings drawer (Task #22).

    ``resolutions`` and ``fps_options`` are the set of modes the backend
    is willing to accept (v4l2 + realsense modes are a static superset;
    a future version may query the driver directly).  ``current`` reflects
    the values stored in the registry entry, not necessarily what the
    hardware currently reports.

    ``source`` lets the UI hide controls that don't apply — e.g. codec
    choice is irrelevant for the ``fake`` source.
    """

    model_config = ConfigDict(extra="forbid")

    resolutions: list[Resolution]
    fps_options: list[float]
    codecs: list[str]
    current: CurrentMode
    source: CameraSourceType


def _capabilities_for(entry: CameraEntry) -> CameraCapabilities:
    """Build a :class:`CameraCapabilities` from a registry entry.

    Resolution / fps / codec lists are currently static per backend.
    When the camera is open, a future version can query the v4l2 driver
    (``v4l2-ctl --list-formats-ext``) or the RealSense SDK to advertise
    the exact supported modes.
    """
    if entry.backend == "realsense":
        source: CameraSourceType = "realsense"
        codecs = _CODECS_REALSENSE
    elif entry.backend == "opencv":
        source = "v4l2"
        codecs = _CODECS_V4L2
    else:
        # "network" and any future backends fall back to the fake source
        source = "fake"
        codecs = _CODECS_FAKE

    # Ensure the entry's current resolution is always in the list so the
    # picker can show it even if it falls outside the standard set.
    resolutions = list(_STANDARD_RESOLUTIONS)
    current_res = {"width": entry.width, "height": entry.height}
    if current_res not in resolutions:
        resolutions.insert(0, current_res)

    fps_options = list(_STANDARD_FPS)
    if entry.fps not in fps_options:
        fps_options = sorted({entry.fps} | set(fps_options))

    return CameraCapabilities(
        resolutions=[Resolution(**r) for r in resolutions],
        fps_options=fps_options,
        codecs=codecs,
        current=CurrentMode(
            width=entry.width,
            height=entry.height,
            fps=entry.fps,
            codec=entry.codec_hint,
        ),
        source=source,
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


@router.get("/{camera_id}/capabilities", response_model=CameraCapabilities)
async def get_camera_capabilities(
    camera_id: UUID,
    registry: Registry = Depends(get_registry),
) -> CameraCapabilities:
    """Return the advertised modes and current settings for the settings drawer.

    ``resolutions``, ``fps_options``, and ``codecs`` are the superset of
    modes the backend supports for this camera type. The UI uses ``source``
    to conditionally hide controls that don't apply (e.g. codec for fake
    sources). A future version may query the v4l2 or RealSense driver live
    when the camera is open.
    """
    try:
        entry = await registry.get_camera(camera_id)
    except RegistryError as exc:
        raise map_registry_error(exc) from exc
    return _capabilities_for(entry)
