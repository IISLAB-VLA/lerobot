"""Assembles the top-level ``/api`` router from feature subrouters."""

from __future__ import annotations

from fastapi import APIRouter

from lerobot.dashboard.api import cameras, devices, health, robots, teleops
from lerobot.dashboard.api import calibration  # noqa: I001 — must come after others to avoid teleop circular import
from lerobot.dashboard.streaming import build_streams_router


def build_api_router() -> APIRouter:
    """Return the mounted ``/api`` router with every feature subrouter attached."""
    router = APIRouter(prefix="/api")
    router.include_router(health.router)
    router.include_router(devices.router)
    router.include_router(robots.router)
    router.include_router(cameras.router)
    router.include_router(teleops.router)
    router.include_router(calibration.router)
    router.include_router(build_streams_router())
    return router
