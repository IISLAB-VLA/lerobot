"""Shared application state held for the lifetime of the FastAPI app."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lerobot.dashboard.core.config import DashboardConfig

if TYPE_CHECKING:
    from lerobot.dashboard.services.assets import AssetManager
    from lerobot.dashboard.services.camera_manager import CameraManagerProtocol
    from lerobot.dashboard.services.recorder import RecorderService
    from lerobot.dashboard.services.registry import Registry
    from lerobot.dashboard.services.robot_manager import RobotManagerProtocol
    from lerobot.dashboard.services.teleop_manager import TeleopManagerProtocol
    from lerobot.dashboard.streaming import SignalingManager


@dataclass(slots=True)
class AppState:
    """Process-wide runtime state for the dashboard server.

    Sub-modules (registry, WebRTC pool, teleop hub, etc.) attach their
    own handles to this object as they come online.
    """

    config: DashboardConfig
    startup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    registry: Registry | None = None
    robot_manager: RobotManagerProtocol | None = None
    camera_manager: CameraManagerProtocol | None = None
    teleop_manager: TeleopManagerProtocol | None = None
    streaming: SignalingManager | None = None
    recorder: RecorderService | None = None
    assets: AssetManager | None = None
    live_robots: dict[str, Any] = field(default_factory=dict)
    live_cameras: dict[str, Any] = field(default_factory=dict)
    live_teleops: dict[str, Any] = field(default_factory=dict)
