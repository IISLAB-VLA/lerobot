"""CLI + env config wiring regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lerobot.dashboard.cli import _build_parser, _config_from_args
from lerobot.dashboard.core.config import (
    ENV_CORS_ORIGINS,
    ENV_FAKE_DEVICES,
    ENV_FAKE_POLICY,
    ENV_LOG_LEVEL,
    ENV_STATIC_DIR,
    ENV_STORAGE_DIR,
    DashboardConfig,
)


@pytest.fixture(autouse=True)
def _clear_dashboard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a clean env so neighbours can't bleed in."""
    for var in (
        ENV_STORAGE_DIR,
        ENV_STATIC_DIR,
        ENV_CORS_ORIGINS,
        ENV_LOG_LEVEL,
        ENV_FAKE_DEVICES,
        ENV_FAKE_POLICY,
    ):
        monkeypatch.delenv(var, raising=False)


def test_env_only_static_dir_survives_cli_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "frontend-dist"
    static.mkdir()
    monkeypatch.setenv(ENV_STATIC_DIR, str(static))

    args = _build_parser().parse_args([])
    config = _config_from_args(args)

    assert config.static_dir == static


def test_cli_static_dir_overrides_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_static = tmp_path / "env-dist"
    cli_static = tmp_path / "cli-dist"
    env_static.mkdir()
    cli_static.mkdir()
    monkeypatch.setenv(ENV_STATIC_DIR, str(env_static))

    args = _build_parser().parse_args(["--static-dir", str(cli_static)])
    config = _config_from_args(args)

    assert config.static_dir == cli_static


def test_export_to_env_preserves_existing_static_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "externally-set"
    monkeypatch.setenv(ENV_STATIC_DIR, str(external))

    DashboardConfig(storage_dir=tmp_path / "storage", static_dir=None).export_to_env()

    import os

    assert os.environ.get(ENV_STATIC_DIR) == str(external)


def test_fake_flags_round_trip_through_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FAKE_DEVICES, "1")
    monkeypatch.setenv(ENV_FAKE_POLICY, "1")

    args = _build_parser().parse_args([])
    config = _config_from_args(args)

    assert config.fake_devices is True
    assert config.fake_policy is True
