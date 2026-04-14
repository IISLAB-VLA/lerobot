"""Shared FastAPI dependencies for the ``/api`` layer."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from lerobot.dashboard.services.registry import (
    Registry,
    RegistryError,
    RegistryNotFoundError,
    RegistryValidationError,
)
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol


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


def map_registry_error(exc: RegistryError) -> HTTPException:
    if isinstance(exc, RegistryNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, RegistryValidationError):
        # Use the literal 422 to avoid the HTTP_422_UNPROCESSABLE_ENTITY/CONTENT
        # naming churn between starlette versions.
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
