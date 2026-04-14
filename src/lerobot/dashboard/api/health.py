"""Health-check endpoint."""

from __future__ import annotations

import platform
import time
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from lerobot.dashboard import __version__

router = APIRouter(tags=["health"])

_BOOT_MONOTONIC = time.monotonic()


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    python: str
    uptime_seconds: float


@router.get("/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    return HealthResponse(
        version=__version__,
        python=platform.python_version(),
        uptime_seconds=round(time.monotonic() - _BOOT_MONOTONIC, 3),
    )
