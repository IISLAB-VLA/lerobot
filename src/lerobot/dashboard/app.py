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
from lerobot.dashboard.storage.paths import ensure_storage_dir
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

    state = AppState(config=resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "dashboard starting (storage=%s, static=%s, cors=%s)",
            resolved.storage_dir,
            resolved.static_dir,
            list(resolved.cors_origins),
        )
        try:
            yield
        finally:
            logger.info("dashboard shutting down")

    app = FastAPI(
        title="LeRobot Dashboard",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.dashboard = state

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(build_api_router())
    app.include_router(build_ws_router())

    if resolved.static_dir is not None and resolved.static_dir.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(resolved.static_dir), html=True),
            name="frontend",
        )
    elif resolved.static_dir is not None:
        logger.warning(
            "static_dir %s does not exist; skipping frontend mount", resolved.static_dir
        )

    return app
