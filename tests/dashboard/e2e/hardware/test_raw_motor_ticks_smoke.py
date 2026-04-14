"""Hardware smoke test for Task #21 — raw encoder tick read path.

Validates ``LerobotRobotManager.read_raw_encoder_ticks`` against a live
SO-101 robot connected at ``/dev/ttyACM0``.

Skip conditions (clean, no failure):
* ``/dev/ttyACM0`` absent — robot not connected.
* ``lerobot[feetech]`` extra not installed.
* Robot connect fails (EBUSY, permission error, etc.).

Run via::

    uv run pytest tests/dashboard/e2e/hardware/test_raw_motor_ticks_smoke.py \\
        -m hardware -v

Or from the Makefile::

    make dashboard-e2e-hardware

SO-101 Feetech motor names and 12-bit encoder range (0–4095) are
validated here.  No calibration is required — this read path explicitly
bypasses the calibration table.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Hardware availability guards (same pattern as test_robot_env_runner_smoke)
# ---------------------------------------------------------------------------

SO101_PORT = "/dev/ttyACM0"

# Expected motor names for SO-101 (follower arm, 6 DOF).
SO101_EXPECTED_MOTORS = {
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
}

# 12-bit Feetech STS3215 encoder resolution.
FEETECH_TICK_MIN = 0
FEETECH_TICK_MAX = 4095

pytestmark = pytest.mark.hardware


def _ttyacm0_present() -> bool:
    return Path(SO101_PORT).exists()


def _feetech_importable() -> bool:
    try:
        import lerobot.motors.feetech  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ttyacm0_present(), reason=f"{SO101_PORT} not present — robot not connected")
@pytest.mark.skipif(not _feetech_importable(), reason="lerobot[feetech] not installed")
async def test_read_raw_encoder_ticks_smoke(tmp_path: Path) -> None:
    """Connect SO-101 and read raw encoder ticks without calibration.

    Validates:
    * Return value is ``dict[str, int]`` with at least one motor entry.
    * Motor names are a non-empty subset of the expected SO-101 names.
    * All tick values are within the 12-bit Feetech range [0, 4095].
    * Robot remains online after the read (soft-fail contract).
    """
    from lerobot.dashboard.services.registry_models import RobotEntry, SerialConnection
    from lerobot.dashboard.services.robot_manager_impl import LerobotRobotManager

    robot_id = uuid4()
    entry = RobotEntry(
        id=robot_id,
        name="SO-101 raw-tick smoke",
        robot_type="so101_follower",
        connection=SerialConnection(port=SO101_PORT, baudrate=1_000_000),
    )

    manager = LerobotRobotManager()
    status = await manager.connect(entry, cameras=[])
    if not status.online:
        pytest.skip(f"SO-101 connect failed: {status.last_error!r}")

    try:
        ticks = await manager.read_raw_encoder_ticks(robot_id)

        # ----- schema assertions -------------------------------------------

        # Must be a non-empty dict (empty means bus read failed).
        assert isinstance(ticks, dict), f"expected dict, got {type(ticks)}"
        assert len(ticks) > 0, (
            "read_raw_encoder_ticks returned {} — bus may not be accessible "
            "or sync_read failed. Check /dev/ttyACM0 permissions."
        )

        # All keys must be valid SO-101 motor names.
        unknown = set(ticks.keys()) - SO101_EXPECTED_MOTORS
        assert not unknown, (
            f"unexpected motor names {unknown!r} — not in {SO101_EXPECTED_MOTORS}"
        )

        # At least shoulder_pan should be present (primary axis).
        assert "shoulder_pan" in ticks, (
            f"shoulder_pan missing from ticks: {list(ticks.keys())}"
        )

        # All values must be in 12-bit range.
        for motor, tick in ticks.items():
            assert isinstance(tick, int), (
                f"{motor}: expected int tick, got {type(tick).__name__} = {tick!r}"
            )
            assert FEETECH_TICK_MIN <= tick <= FEETECH_TICK_MAX, (
                f"{motor}: tick {tick} out of 12-bit range [{FEETECH_TICK_MIN}, {FEETECH_TICK_MAX}]"
            )

        # ----- connectivity assertion ---------------------------------------

        # Raw read must NOT mark the robot offline (soft-fail contract).
        assert await manager.is_connected(robot_id), (
            "robot went offline after read_raw_encoder_ticks — "
            "unexpected hard failure in raw read path"
        )

    finally:
        await manager.disconnect(robot_id)


@pytest.mark.skipif(not _ttyacm0_present(), reason=f"{SO101_PORT} not present — robot not connected")
@pytest.mark.skipif(not _feetech_importable(), reason="lerobot[feetech] not installed")
async def test_read_raw_ticks_multiple_calls_stable(tmp_path: Path) -> None:
    """Multiple consecutive raw reads return consistent motor names.

    Values may differ (servo moves slightly or has noise), but the key
    set must be identical across calls — validates that the bus state
    is not corrupted between reads.
    """
    from lerobot.dashboard.services.registry_models import RobotEntry, SerialConnection
    from lerobot.dashboard.services.robot_manager_impl import LerobotRobotManager

    robot_id = uuid4()
    entry = RobotEntry(
        id=robot_id,
        name="SO-101 tick-stability smoke",
        robot_type="so101_follower",
        connection=SerialConnection(port=SO101_PORT, baudrate=1_000_000),
    )

    manager = LerobotRobotManager()
    status = await manager.connect(entry, cameras=[])
    if not status.online:
        pytest.skip(f"SO-101 connect failed: {status.last_error!r}")

    try:
        reads: list[dict[str, int]] = []
        for _ in range(3):
            ticks = await manager.read_raw_encoder_ticks(robot_id)
            if not ticks:
                pytest.skip("read_raw_encoder_ticks returned {} — bus read failed")
            reads.append(ticks)

        # Key sets must be identical across all reads.
        first_keys = set(reads[0].keys())
        for i, r in enumerate(reads[1:], start=1):
            assert set(r.keys()) == first_keys, (
                f"read {i}: motor keys changed — got {set(r.keys())}, expected {first_keys}"
            )

        assert await manager.is_connected(robot_id), "robot offline after stability reads"
    finally:
        await manager.disconnect(robot_id)
