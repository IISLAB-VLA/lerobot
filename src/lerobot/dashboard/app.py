"""FastAPI application factory for the LeRobot dashboard."""

from __future__ import annotations

import logging
import logging.config
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from lerobot.dashboard.api.router import build_api_router
from lerobot.dashboard.core.config import DashboardConfig
from lerobot.dashboard.core.state import AppState
from lerobot.dashboard.services.assets import RESOURCE_URL_PREFIX, AssetManager
from lerobot.dashboard.services.benchmark import BenchmarkController
from lerobot.dashboard.services.calibration import CalibrationController
from lerobot.dashboard.services.camera_manager import InMemoryCameraManager, LerobotCameraManager
from lerobot.dashboard.services.inference import InferenceService
from lerobot.dashboard.services.recorder import RecorderService
from lerobot.dashboard.services.registry import Registry, registry_path_for
from lerobot.dashboard.services.robot_manager import InMemoryRobotManager
from lerobot.dashboard.services.robot_manager_impl import LerobotRobotManager
from lerobot.dashboard.services.teleop_manager import InMemoryTeleopManager
from lerobot.dashboard.storage.paths import ensure_storage_dir
from lerobot.dashboard.streaming import (
    RegistryFrameSourceProvider,
    SignalingManager,
)
from lerobot.dashboard.ws.router import build_ws_router

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    normalized = level.upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    "datefmt": "%H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "level": normalized,
                },
            },
            "root": {"level": normalized, "handlers": ["console"]},
            "loggers": {
                "uvicorn": {"level": normalized, "handlers": [], "propagate": True},
                "uvicorn.access": {"level": normalized, "handlers": [], "propagate": True},
                "uvicorn.error": {"level": normalized, "handlers": [], "propagate": True},
            },
        }
    )


def create_app(config: DashboardConfig | None = None) -> FastAPI:
    """Build and return the dashboard FastAPI app.

    When invoked via ``uvicorn.run(..., factory=True)`` the ``config``
    argument is not supplied and we re-hydrate from environment variables
    written by the CLI.
    """
    resolved = config or DashboardConfig.from_env()
    _configure_logging(resolved.log_level)
    ensure_storage_dir(resolved.storage_dir)

    assets = AssetManager(resolved.storage_dir)
    state = AppState(config=resolved, assets=assets)
    state.registry = Registry(registry_path_for(resolved.storage_dir))
    if resolved.fake_devices:
        state.robot_manager = InMemoryRobotManager()
        state.camera_manager = InMemoryCameraManager()
    else:
        state.robot_manager = LerobotRobotManager()
        state.camera_manager = LerobotCameraManager()
    state.teleop_manager = InMemoryTeleopManager()
    state.calibration = CalibrationController()
    state.benchmark = BenchmarkController(storage_dir=resolved.storage_dir / "benchmarks")
    # Streaming bridges the camera_manager's ``subscribe()`` into the
    # WebRTC track. In ``fake_devices`` mode ``state.camera_manager`` is
    # the in-memory fallback so the wiring still succeeds — frames are
    # just zero-filled placeholders instead of real captures.
    state.streaming = SignalingManager(
        RegistryFrameSourceProvider(
            manager=state.camera_manager,
            registry=state.registry,
        )
    )
    state.recorder = RecorderService(
        registry=state.registry,
        robot_manager=state.robot_manager,
        camera_manager=state.camera_manager,
        datasets_dir=resolved.resolved_dataset_dir(),
    )
    state.inference = InferenceService(
        registry=state.registry,
        robot_manager=state.robot_manager,
        camera_manager=state.camera_manager,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "dashboard starting (storage=%s, static=%s, cors=%s, fake_devices=%s, fake_policy=%s)",
            resolved.storage_dir,
            resolved.static_dir,
            list(resolved.cors_origins),
            resolved.fake_devices,
            resolved.fake_policy,
        )
        assert state.registry is not None
        await state.registry.load()
        try:
            yield
        finally:
            logger.info("dashboard shutting down")
            if state.inference is not None:
                await state.inference.close()
            if state.recorder is not None:
                await state.recorder.close()
            if state.streaming is not None:
                await state.streaming.close_all()

    app = FastAPI(
        title="LeRobot Dashboard",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.dashboard = state
    # The streaming router reads the manager directly off ``app.state.streaming``;
    # mirror it so we don't leak the AppState indirection into that package.
    app.state.streaming = state.streaming

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(build_api_router())
    app.include_router(build_ws_router())

    app.mount(
        RESOURCE_URL_PREFIX,
        StaticFiles(directory=str(assets.robots_dir)),
        name="robot-images",
    )

    if resolved.static_dir is not None and resolved.static_dir.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(resolved.static_dir), html=True),
            name="frontend",
        )
    elif resolved.static_dir is not None:
        logger.warning("static_dir %s does not exist; skipping frontend mount", resolved.static_dir)

    return app
