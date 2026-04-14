"""Shared FastAPI dependencies for the ``/api`` layer."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from lerobot.dashboard.services.assets import AssetManager
from lerobot.dashboard.services.camera_manager import CameraManagerProtocol
from lerobot.dashboard.services.recorder import (
    RecorderConflictError,
    RecorderError,
    RecorderNotFoundError,
    RecorderService,
    RecorderValidationError,
)
from lerobot.dashboard.services.registry import (
    Registry,
    RegistryError,
    RegistryNotFoundError,
    RegistryValidationError,
)
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol
from lerobot.dashboard.services.teleop_manager import TeleopManagerProtocol


def get_registry(request: Request) -> Registry:
    state = request.app.state.dashboard
    registry = state.registry
    if registry is None:
        # Should never happen — wiring is set up in ``create_app``.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="registry is not initialized",
        )
    return registry


def get_robot_manager(request: Request) -> RobotManagerProtocol:
    state = request.app.state.dashboard
    manager = state.robot_manager
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="robot manager is not initialized",
        )
    return manager


def get_camera_manager(request: Request) -> CameraManagerProtocol:
    state = request.app.state.dashboard
    manager = state.camera_manager
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="camera manager is not initialized",
        )
    return manager


def get_assets(request: Request) -> AssetManager:
    state = request.app.state.dashboard
    assets = state.assets
    if assets is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="asset manager is not initialized",
        )
    return assets


def get_teleop_manager(request: Request) -> TeleopManagerProtocol:
    state = request.app.state.dashboard
    manager = state.teleop_manager
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="teleop manager is not initialized",
        )
    return manager


def map_registry_error(exc: RegistryError) -> HTTPException:
    if isinstance(exc, RegistryNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, RegistryValidationError):
        # Use the literal 422 to avoid the HTTP_422_UNPROCESSABLE_ENTITY/CONTENT
        # naming churn between starlette versions.
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


def get_recorder(request: Request) -> RecorderService:
    state = request.app.state.dashboard
    recorder = state.recorder
    if recorder is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="recorder is not initialized",
        )
    return recorder


def map_recorder_error(exc: RecorderError) -> HTTPException:
    if isinstance(exc, RecorderNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, RecorderValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, RecorderConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
