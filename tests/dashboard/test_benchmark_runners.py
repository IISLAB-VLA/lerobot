"""Phase 2a runner tests.

Uses a fake gym vector env so the suite stays gym-package-free. The
trajectory parquet is read back with pyarrow to assert the schema.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from lerobot.dashboard.services.benchmark_runners import (
    RandomActionEnvRunner,
    TrajectoryWriter,
)


# ---------------------------------------------------------------------------
# Fake gym vector env (mimics gym.vector.VectorEnv minimally)
# ---------------------------------------------------------------------------


class _FakeBox:
    def __init__(self, shape: tuple[int, ...]) -> None:
        self.shape = shape

    def sample(self) -> np.ndarray:
        return np.zeros(self.shape, dtype=np.float32)


class _FakeVecEnv:
    """Single-env vector wrapper that terminates after ``terminate_at`` steps."""

    def __init__(self, action_dim: int = 2, terminate_at: int = 4, with_image: bool = False) -> None:
        self.single_action_space = _FakeBox(shape=(action_dim,))
        self.action_space = _FakeBox(shape=(1, action_dim))
        self._steps = 0
        self._terminate_at = terminate_at
        self._closed = False
        self._with_image = with_image

    def reset(self, seed: int | None = None) -> tuple:
        self._steps = 0
        return self._make_obs(), {}

    def step(self, action: np.ndarray) -> tuple:
        self._steps += 1
        terminated = np.array([self._steps >= self._terminate_at])
        truncated = np.array([False])
        reward = np.array([float(self._steps) * 0.1], dtype=np.float32)
        return self._make_obs(), reward, terminated, truncated, {}

    def _make_obs(self):
        if self._with_image:
            # gym dict obs with a (1, H, W, 3) pixels array — Pusht-shaped.
            pixels = np.full((1, 8, 8, 3), self._steps % 256, dtype=np.uint8)
            agent_pos = np.zeros((1, 2), dtype=np.float32)
            return {"pixels": pixels, "agent_pos": agent_pos}
        return np.full((1, 8), float(self._steps), dtype=np.float32)

    def close(self) -> None:
        self._closed = True


def _fake_factory(env_name: str) -> dict:
    return {env_name: {0: _FakeVecEnv(action_dim=2, terminate_at=3)}}


# ---------------------------------------------------------------------------
# _Run shim — mirrors the controller's internal dataclass
# ---------------------------------------------------------------------------


@dataclass
class _RunShim:
    run_id: str
    env_name: str
    episodes: int
    seed: int | None
    storage_dir: Path
    cancelled: bool = False
    steps_total: int = 0
    progress: float = 0.0
    current_episode: int = 0
    last_reward: float | None = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_writer_writes_action_columns(tmp_path: Path) -> None:
    writer = TrajectoryWriter(
        path=tmp_path / "trajectory.parquet",
        run_id="run-x",
        action_keys=["a0", "a1"],
    )
    await writer.append(
        policy_slug="random",
        episode=0,
        step=0,
        reward=0.5,
        done=False,
        truncated=False,
        action=[0.1, -0.2],
    )
    await writer.close()

    table = pq.read_table(tmp_path / "trajectory.parquet")
    cols = table.column_names
    assert "run_id" in cols
    assert "action_a0" in cols
    assert "action_a1" in cols
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["policy_slug"] == "random"
    assert row["action_a0"] == pytest.approx(0.1)


async def test_runner_records_one_episode_until_terminated(tmp_path: Path) -> None:
    run = _RunShim(
        run_id="run-test",
        env_name="pusht",
        episodes=1,
        seed=42,
        storage_dir=tmp_path,
    )
    events: list[dict] = []

    async def publish(_run: _RunShim, event: dict) -> None:
        events.append(event)

    runner = RandomActionEnvRunner(env_factory=_fake_factory, max_steps_per_episode=10)
    await runner(run, publish)

    types = [e["type"] for e in events]
    assert "step" in types
    # Fake env terminates after 3 steps.
    step_events = [e for e in events if e["type"] == "step"]
    assert len(step_events) == 3
    assert step_events[-1]["done"] is True
    assert step_events[-1]["policy_slug"] == "random"
    assert step_events[-1]["episode"] == 0

    table = pq.read_table(tmp_path / "trajectory.parquet")
    assert table.num_rows == 3
    assert "action_a0" in table.column_names


async def test_runner_runs_full_requested_episodes(tmp_path: Path) -> None:
    run = _RunShim(
        run_id="run-multi",
        env_name="pusht",
        episodes=3,
        seed=0,
        storage_dir=tmp_path,
    )
    events: list[dict] = []

    async def publish(_run: _RunShim, event: dict) -> None:
        events.append(event)

    runner = RandomActionEnvRunner(env_factory=_fake_factory, max_steps_per_episode=10)
    await runner(run, publish)

    table = pq.read_table(tmp_path / "trajectory.parquet")
    episodes_in_parquet = set(table.column("episode").to_pylist())
    assert episodes_in_parquet == {0, 1, 2}
    assert run.current_episode == 2
    assert run.progress == pytest.approx(1.0)


async def test_runner_seed_offsets_per_episode(tmp_path: Path) -> None:
    seeds_seen: list[int | None] = []

    class _SeedTrackingEnv(_FakeVecEnv):
        def reset(self, seed: int | None = None) -> tuple:
            seeds_seen.append(seed)
            return super().reset(seed=seed)

    def _factory(env_name: str) -> dict:
        return {env_name: {0: _SeedTrackingEnv(action_dim=1, terminate_at=1)}}

    run = _RunShim(
        run_id="run-seeds",
        env_name="pusht",
        episodes=3,
        seed=100,
        storage_dir=tmp_path,
    )
    runner = RandomActionEnvRunner(env_factory=_factory)

    async def publish(_run: _RunShim, event: dict) -> None:
        pass

    await runner(run, publish)
    assert seeds_seen == [100, 101, 102]


async def test_runner_propagates_env_package_missing(tmp_path: Path) -> None:
    def _broken_factory(env_name: str) -> dict:
        raise ImportError(f"No module named gym_{env_name}")

    run = _RunShim(
        run_id="run-missing",
        env_name="pusht",
        episodes=1,
        seed=None,
        storage_dir=tmp_path,
    )
    events: list[dict] = []

    async def publish(_run: _RunShim, event: dict) -> None:
        events.append(event)

    runner = RandomActionEnvRunner(env_factory=_broken_factory)
    with pytest.raises(RuntimeError, match="not installed"):
        await runner(run, publish)
    assert events and events[-1]["type"] == "error"
    assert events[-1]["code"] == "EnvPackageMissing"


async def test_runner_writes_observation_jpgs_when_obs_contains_image(tmp_path: Path) -> None:
    def _factory(env_name: str) -> dict:
        return {env_name: {0: _FakeVecEnv(action_dim=2, terminate_at=2, with_image=True)}}

    run = _RunShim(
        run_id="run-img",
        env_name="pusht",
        episodes=1,
        seed=7,
        storage_dir=tmp_path,
    )
    captured: list[dict] = []

    async def publish(_run: _RunShim, event: dict) -> None:
        captured.append(event)

    runner = RandomActionEnvRunner(env_factory=_factory, max_steps_per_episode=10)
    await runner(run, publish)

    table = pq.read_table(tmp_path / "trajectory.parquet")
    assert "obs_jpg_path" in table.column_names
    paths = table.column("obs_jpg_path").to_pylist()
    assert paths == ["observations/episode_0/step_0.jpg", "observations/episode_0/step_1.jpg"]
    for rel in paths:
        assert (tmp_path / rel).is_file()
        # File should be a JPG header (FFD8FF).
        head = (tmp_path / rel).read_bytes()[:3]
        assert head == b"\xff\xd8\xff"

    # WS step events carry a ready-to-use absolute URL alongside the parquet path.
    step_events = [e for e in captured if e["type"] == "step"]
    assert all("obs_jpg_url" in e for e in step_events)
    assert step_events[0]["obs_jpg_url"] == (
        "/resources/benchmarks/run-img/observations/episode_0/step_0.jpg"
    )


async def test_runner_obs_jpg_path_null_when_no_image_in_obs(tmp_path: Path) -> None:
    run = _RunShim(
        run_id="run-noimg",
        env_name="pusht",
        episodes=1,
        seed=0,
        storage_dir=tmp_path,
    )

    async def publish(_run: _RunShim, event: dict) -> None:
        pass

    runner = RandomActionEnvRunner(env_factory=_fake_factory, max_steps_per_episode=10)
    await runner(run, publish)
    table = pq.read_table(tmp_path / "trajectory.parquet")
    assert all(p is None for p in table.column("obs_jpg_path").to_pylist())
    # No observations directory should be created.
    assert not (tmp_path / "observations").exists()


async def test_runner_honours_cancellation_between_steps(tmp_path: Path) -> None:
    run = _RunShim(
        run_id="run-cancel",
        env_name="pusht",
        episodes=5,
        seed=0,
        storage_dir=tmp_path,
    )

    captured: list[dict] = []

    async def publish(_run: _RunShim, event: dict) -> None:
        captured.append(event)
        if len(captured) == 2:
            run.cancelled = True

    runner = RandomActionEnvRunner(
        env_factory=lambda name: {name: {0: _FakeVecEnv(action_dim=1, terminate_at=999)}},
        max_steps_per_episode=999,
    )
    await runner(run, publish)
    # Allow one extra step to flush the publish that triggered cancellation.
    assert len(captured) <= 4
