"""Runtime configuration for the dashboard server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from lerobot.dashboard.storage.paths import default_storage_dir

ENV_STORAGE_DIR = "LEROBOT_DASHBOARD_STORAGE_DIR"
ENV_STATIC_DIR = "LEROBOT_DASHBOARD_STATIC_DIR"
ENV_DATASET_DIR = "LEROBOT_DASHBOARD_DATASET_DIR"
ENV_CORS_ORIGINS = "LEROBOT_DASHBOARD_CORS_ORIGINS"
ENV_LOG_LEVEL = "LEROBOT_DASHBOARD_LOG_LEVEL"
ENV_FAKE_DEVICES = "LEROBOT_DASHBOARD_FAKE_DEVICES"
ENV_FAKE_POLICY = "LEROBOT_DASHBOARD_FAKE_POLICY"


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_DEFAULT_DEV_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)


@dataclass(slots=True)
class DashboardConfig:
    """Resolved configuration for a dashboard server instance.

    The CLI builds this from argparse, writes the values to environment
    variables, and the uvicorn factory re-hydrates them via :meth:`from_env`.
    That indirection keeps the config flowing correctly across uvicorn
    reload workers, which re-import the app module.
    """

    host: str = "0.0.0.0"  # noqa: S104  # nosec B104 — dashboard is intentionally LAN-reachable
    port: int = 8080
    reload: bool = False
    storage_dir: Path = field(default_factory=default_storage_dir)
    static_dir: Path | None = None
    dataset_dir: Path | None = None
    cors_origins: tuple[str, ...] = _DEFAULT_DEV_ORIGINS
    log_level: str = "info"
    fake_devices: bool = field(default_factory=lambda: _parse_bool(os.environ.get(ENV_FAKE_DEVICES)))
    fake_policy: bool = field(default_factory=lambda: _parse_bool(os.environ.get(ENV_FAKE_POLICY)))

    def export_to_env(self) -> None:
        """Write config values into ``os.environ`` for the uvicorn factory.

        Only populated values are written. We never ``pop`` an existing env
        variable here — if the caller wants ``static_dir`` unset, they must
        clear the env themselves; silently erasing what the user set would
        make env-driven deployments (systemd, docker) surprising.
        """
        os.environ[ENV_STORAGE_DIR] = str(self.storage_dir)
        if self.static_dir is not None:
            os.environ[ENV_STATIC_DIR] = str(self.static_dir)
        if self.dataset_dir is not None:
            os.environ[ENV_DATASET_DIR] = str(self.dataset_dir)
        os.environ[ENV_CORS_ORIGINS] = ",".join(self.cors_origins)
        os.environ[ENV_LOG_LEVEL] = self.log_level
        os.environ[ENV_FAKE_DEVICES] = "1" if self.fake_devices else "0"
        os.environ[ENV_FAKE_POLICY] = "1" if self.fake_policy else "0"

    @classmethod
    def from_env(cls) -> DashboardConfig:
        """Re-hydrate a :class:`DashboardConfig` from environment variables."""
        storage_dir_raw = os.environ.get(ENV_STORAGE_DIR)
        storage_dir = Path(storage_dir_raw).expanduser() if storage_dir_raw else default_storage_dir()

        static_dir_raw = os.environ.get(ENV_STATIC_DIR)
        static_dir = Path(static_dir_raw).expanduser() if static_dir_raw else None

        dataset_dir_raw = os.environ.get(ENV_DATASET_DIR)
        dataset_dir = Path(dataset_dir_raw).expanduser() if dataset_dir_raw else None

        origins_raw = os.environ.get(ENV_CORS_ORIGINS)
        if origins_raw:
            origins = tuple(o.strip() for o in origins_raw.split(",") if o.strip())
        else:
            origins = _DEFAULT_DEV_ORIGINS

        log_level = os.environ.get(ENV_LOG_LEVEL, "info")

        return cls(
            storage_dir=storage_dir,
            static_dir=static_dir,
            dataset_dir=dataset_dir,
            cors_origins=origins,
            log_level=log_level,
            fake_devices=_parse_bool(os.environ.get(ENV_FAKE_DEVICES)),
            fake_policy=_parse_bool(os.environ.get(ENV_FAKE_POLICY)),
        )

    def resolved_dataset_dir(self) -> Path:
        """Directory under which LeRobotDataset recordings land.

        Defaults to ``<storage_dir>/datasets`` and can be overridden via the
        ``dataset_dir`` field (or ``LEROBOT_DASHBOARD_DATASET_DIR`` env var).
        """
        return self.dataset_dir if self.dataset_dir is not None else self.storage_dir / "datasets"
