"""Default on-disk paths for dashboard state."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_STORAGE_DIR = "LEROBOT_DASHBOARD_STORAGE_DIR"


def default_storage_dir() -> Path:
    """Return the default dashboard storage directory.

    Resolution order:
        1. ``$LEROBOT_DASHBOARD_STORAGE_DIR`` if set.
        2. ``$XDG_CACHE_HOME/lerobot/dashboard`` if ``XDG_CACHE_HOME`` is set.
        3. ``~/.cache/lerobot/dashboard``.
    """
    override = os.environ.get(_ENV_STORAGE_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "lerobot" / "dashboard"


def ensure_storage_dir(path: Path | None = None) -> Path:
    """Create the storage directory (and parents) if missing and return it."""
    resolved = path or default_storage_dir()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
