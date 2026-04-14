"""Assembles the top-level ``/api`` router from feature subrouters."""

from __future__ import annotations

from fastapi import APIRouter

from lerobot.dashboard.api import health


def build_api_router() -> APIRouter:
    """Return the mounted ``/api`` router with every feature subrouter attached."""
    router = APIRouter(prefix="/api")
    router.include_router(health.router)
    return router
