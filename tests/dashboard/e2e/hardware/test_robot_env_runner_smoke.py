"""Hardware smoke test for RobotEnvRunner + SO-101.

Runs 3 hold-position steps through the real LerobotRobotManager pipeline
to validate the end-to-end benchmark recording path on physical hardware.

Skip conditions (clean, no failure):
* ``/dev/ttyACM0`` absent — robot not connected.
* ``feetech`` extra not installed — import guard.
* Robot connect fails — EBUSY or calibration error.

Run via::

    uv run pytest tests/dashboard/e2e/hardware/test_robot_env_runner_smoke.py \
        -m hardware -v

Or from the Makefile::

    make dashboard-e2e-hardware
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pyarrow.parquet as pq
import pytest

# ---------------------------------------------------------------------------
# Hardware availability guards
# ---------------------------------------------------------------------------

SO101_PORT = "/dev/ttyACM0"


def _ttyacm0_present() -> bool:
    return Path(SO101_PORT).exists()


def _feetech_importable() -> bool:
    try:
        import lerobot.motors.feetech  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = pytest.mark.hardware


# ---------------------------------------------------------------------------
# Minimal _Run shim matching BenchmarkController._Run fields used by runner
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
    fps: int = 10
    policy_refs: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.policy_refs is None:
            self.policy_refs = []


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ttyacm0_present(), reason=f"{SO101_PORT} not present — robot not connected")
@pytest.mark.skipif(not _feetech_importable(), reason="lerobot[feetech] not installed")
async def test_robot_env_runner_hold_position_smoke(tmp_path: Path) -> None:
    """Connect SO-101, run 3 hold-position steps, verify parquet + WS events.

    The robot is NOT moved — hold-position action equals the current
    observed joint positions, so this test is safe to run with the arm
    resting on a surface.
    """
    from lerobot.dashboard.services.benchmark_runners import RobotEnvRunner
    from lerobot.dashboard.services.registry_models import RobotEntry, SerialConnection
    from lerobot.dashboard.services.robot_manager_impl import LerobotRobotManager

    robot_id = uuid4()
    entry = RobotEntry(
        id=robot_id,
        name="SO-101 smoke",
        robot_type="so101_follower",
        connection=SerialConnection(port=SO101_PORT, baudrate=1_000_000),
    )

    manager = LerobotRobotManager()

    # Connect — skip gracefully on EBUSY / calibration errors.
    status = await manager.connect(entry, cameras=[])
    if not status.online:
        pytest.skip(f"SO-101 connect failed: {status.last_error!r}")

    run = _RunShim(
        run_id="smoke-robot-env",
        env_name="so101",
        episodes=1,
        seed=None,
        storage_dir=tmp_path,
        fps=10,
    )
    events: list[dict] = []

    async def publish(_run: _RunShim, event: dict) -> None:
        events.append(event)

    runner = RobotEnvRunner(
        robot_id=robot_id,  # type: ignore[arg-type] — UUID accepted by manager
        robot_manager=manager,
        max_steps_per_episode=3,
    )

    try:
        await runner(run, publish)
    except RuntimeError as exc:
        # SO-101 requires calibration before get_observation works (Task #21).
        # When calibrate=False (dashboard default), the first read_observation
        # returns {} and RobotEnvRunner raises RuntimeError.  Skip instead of
        # failing — the robot is physically connected but not yet calibrated.
        if "empty observation" in str(exc):
            pytest.skip(
                f"SO-101 returned empty observation — robot may need calibration first. "
                f"(Task #21: uncalibrated raw motor read path) Error: {exc}"
            )
        raise
    finally:
        await manager.disconnect(robot_id)

    # ----- assertions -------------------------------------------------------

    step_events = [e for e in events if e["type"] == "step"]
    assert len(step_events) == 3, f"expected 3 step events, got: {[e['type'] for e in events]}"
    assert step_events[-1]["done"] is True
    assert all(e["policy_slug"] == "robot_random" for e in step_events)
    assert all(e["reward"] == pytest.approx(0.0) for e in step_events)

    parquet_path = tmp_path / "trajectory.parquet"
    assert parquet_path.is_file(), "trajectory.parquet not written"
    table = pq.read_table(parquet_path)
    assert table.num_rows == 3
    assert "run_id" in table.column_names
    assert "episode" in table.column_names
    assert table.column("run_id").to_pylist() == ["smoke-robot-env"] * 3

    assert run.progress == pytest.approx(1.0)
    assert run.steps_total == 3
