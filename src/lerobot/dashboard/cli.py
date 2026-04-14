"""Command-line entry point for ``lerobot-dashboard``."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from lerobot.dashboard.core.config import DashboardConfig
from lerobot.dashboard.storage.paths import default_storage_dir


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lerobot-dashboard",
        description="Run the LeRobot web dashboard (FastAPI + uvicorn).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",  # noqa: S104 — dashboard is intentionally LAN-reachable
        help="Interface to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="TCP port (default: 8080)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn autoreload (development)",
    )
    parser.add_argument(
        "--storage-dir",
        type=Path,
        default=None,
        help=f"Persistent storage directory (default: {default_storage_dir()})",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help="Directory of built frontend assets to serve at /",
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=None,
        help="Extra CORS origin to allow (repeatable). Defaults cover localhost dev.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="Log level (default: info)",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> DashboardConfig:
    base = DashboardConfig()
    cors = tuple(args.cors_origin) if args.cors_origin else base.cors_origins
    return DashboardConfig(
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
        storage_dir=args.storage_dir.expanduser() if args.storage_dir else base.storage_dir,
        static_dir=args.static_dir.expanduser() if args.static_dir else None,
        cors_origins=cors,
        log_level=args.log_level,
    )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    config = _config_from_args(args)
    config.export_to_env()
    uvicorn.run(
        "lerobot.dashboard.app:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        reload=config.reload,
        log_level=config.log_level,
    )


if __name__ == "__main__":
    main()
