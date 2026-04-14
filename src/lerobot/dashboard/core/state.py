"""Shared application state held for the lifetime of the FastAPI app."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from lerobot.dashboard.core.config import DashboardConfig


@dataclass(slots=True)
class AppState:
    """Process-wide runtime state for the dashboard server.

    Sub-modules (registry, WebRTC pool, teleop hub, etc.) attach their
    own handles to this object as they come online. The registry fields
    are intentionally ``Any`` stubs for now — task #5 replaces them with
    real :mod:`lerobot.dashboard.services.registry` objects.
    """

    config: DashboardConfig
    startup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    registry: Any | None = None
    assets: Any | None = None
    live_robots: dict[str, Any] = field(default_factory=dict)
    live_cameras: dict[str, Any] = field(default_factory=dict)
    live_teleops: dict[str, Any] = field(default_factory=dict)
