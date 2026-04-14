"""Health-check endpoint."""

from __future__ import annotations

import platform
import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from lerobot.dashboard import __version__

router = APIRouter(tags=["health"])

_BOOT_MONOTONIC = time.monotonic()


class HealthFlags(BaseModel):
    fake_devices: bool = False
    fake_policy: bool = False


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    python: str
    uptime_seconds: float
    flags: HealthFlags


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    state = request.app.state.dashboard
    return HealthResponse(
        version=__version__,
        python=platform.python_version(),
        uptime_seconds=round(time.monotonic() - _BOOT_MONOTONIC, 3),
        flags=HealthFlags(
            fake_devices=state.config.fake_devices,
            fake_policy=state.config.fake_policy,
        ),
    )
