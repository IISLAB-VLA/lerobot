"""Smoke test for Task #14: real smolvla_base policy forward pass end-to-end.

Requires the smolvla_base checkpoint to be locally cached at
``~/.cache/huggingface/hub/models--lerobot--smolvla_base/``.  Marked
``slow`` so it is skipped in the fast CI matrix (no GPU / no cached
weights).

The test exercises:
1. ``scan_hf_cache`` discovers smolvla_base in the local HF cache.
2. ``_default_policy_loader`` loads the weights without error.
3. ``InferenceService.start`` creates a live session with the real policy,
   loading the pre/post-processor pipeline automatically.
4. The inference loop runs at least 2 complete steps (non-dropped) inside
   ``_step_once`` — i.e. the full forward pass through smolvla completes.
5. ``InferenceService.stop`` tears down the session cleanly.

Timing notes
------------
SmolVLA-500M on a cold GPU can take 30-60 s for the first forward pass
(CUDA context init, VLM layer warm-up).  The test therefore uses a
**two-phase deadline**:

* **Phase 1** — ``WARMUP_S`` (120 s): wait for the *first* step to
  complete.  This covers cold GPU start, tokenizer init inside
  ``TokenizerProcessorStep``, and the initial VLM forward pass.
* **Phase 2** — ``STEADY_S`` (10 s): once the GPU is warm the second
  step should arrive well within the next inference period at 2 fps.

The global test deadline is therefore ≈ 130 s.  pytest-timeout has no
global limit configured in ``pyproject.toml``, so the test will not be
killed prematurely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from lerobot.dashboard.services.camera_manager import InMemoryCameraManager
from lerobot.dashboard.services.inference import (
    InferenceService,
    StartRequest,
    scan_hf_cache,
)
from lerobot.dashboard.services.registry import Registry, registry_path_for
from lerobot.dashboard.services.registry_models import (
    RobotEntry,
    SerialConnection,
)
from lerobot.dashboard.services.robot_manager import InMemoryRobotManager

_SMOLVLA_REPO_ID = "lerobot/smolvla_base"
_SMOLVLA_CACHE_ROOT = Path.home() / ".cache" / "huggingface" / "hub" / "models--lerobot--smolvla_base"

# Phase-1: cold GPU + VLM warmup (tokenizer, CUDA context, first forward)
_WARMUP_S = 120.0
# Phase-2: steady-state second step (preprocessors already warm)
_STEADY_S = 10.0


def _smolvla_cached() -> bool:
    """Return True when smolvla_base snapshot is available locally."""
    return _SMOLVLA_CACHE_ROOT.exists() and any(_SMOLVLA_CACHE_ROOT.rglob("config.json"))


pytestmark = pytest.mark.slow


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_path_for(tmp_path / "reg"))


@pytest.fixture
def robot_manager() -> InMemoryRobotManager:
    return InMemoryRobotManager()


@pytest.fixture
def camera_manager() -> InMemoryCameraManager:
    return InMemoryCameraManager()


def _robot_entry() -> RobotEntry:
    return RobotEntry(
        name="so101",
        robot_type="so101_follower",
        connection=SerialConnection(port="/dev/ttyUSB0", baudrate=1_000_000),
        cameras=[],
    )


# ---------------------------------------------------------------------------
# Cache discovery
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _smolvla_cached(), reason="smolvla_base not in local HF cache")
async def test_scan_hf_cache_finds_smolvla() -> None:
    """scan_hf_cache must discover smolvla_base with correct metadata."""
    descriptors = scan_hf_cache()
    ids = [d.repo_id for d in descriptors]
    assert _SMOLVLA_REPO_ID in ids, f"Expected {_SMOLVLA_REPO_ID} in {ids}"
    smolvla = next(d for d in descriptors if d.repo_id == _SMOLVLA_REPO_ID)
    assert smolvla.policy_type == "smolvla"
    assert smolvla.supports_language is True


# ---------------------------------------------------------------------------
# Full forward-pass smoke test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _smolvla_cached(), reason="smolvla_base not in local HF cache")
async def test_smolvla_inference_dry_run_two_steps(
    registry: Registry,
    robot_manager: InMemoryRobotManager,
    camera_manager: InMemoryCameraManager,
) -> None:
    """Real smolvla_base policy must complete ≥2 steps in dry_run mode.

    ``read_observation`` is patched to return a minimal numpy observation
    that satisfies smolvla's input contract:

    * ``observation.state``: float32 array of shape ``(6,)``
    * ``observation.images.camera1``: uint8 array of shape ``(256, 256, 3)``

    The remaining camera inputs (camera2, camera3) are absent; smolvla
    handles missing cameras by creating zero-padded dummy images internally.

    The test uses a two-phase deadline (see module docstring) so it is
    robust against GPU cold-start latency without imposing a tight
    per-step deadline that would false-positive on a warm GPU.
    """

    # Patch read_observation to return proper numpy obs for smolvla
    async def _fake_read_obs(robot_id):  # noqa: ANN001
        return {
            "observation.state": np.zeros(6, dtype=np.float32),
            "observation.images.camera1": np.zeros((256, 256, 3), dtype=np.uint8),
        }

    robot_manager.read_observation = _fake_read_obs  # type: ignore[assignment]

    robot = await registry.create_robot(_robot_entry())
    await robot_manager.connect(robot, [])

    svc = InferenceService(
        registry=registry,
        robot_manager=robot_manager,
        camera_manager=camera_manager,
        # Use real weights from HF cache (no custom policy_loader)
    )

    session = await svc.start(
        StartRequest(
            robot_id=robot.id,
            repo_id=_SMOLVLA_REPO_ID,
            fps=2,  # slow FPS so each step boundary is clearly visible
            task_description="pick the cube",
            dry_run=True,
        )
    )
    try:
        assert session.status == "running", f"expected running, got {session.status!r}"

        # ------------------------------------------------------------------
        # Phase 1: wait for the first step (cold GPU / VLM warmup)
        # ------------------------------------------------------------------
        snapshot = session
        elapsed = 0.0
        poll = 1.0
        while elapsed < _WARMUP_S:
            await asyncio.sleep(poll)
            elapsed += poll
            snapshot = await svc.get(session.id)
            if snapshot.step >= 1:
                break

        assert snapshot.step >= 1, (
            f"First step not completed within {_WARMUP_S:.0f}s.\n"
            f"  status={snapshot.status!r}\n"
            f"  error={snapshot.error!r}\n"
            f"  last_latency_ms={snapshot.last_latency_ms}\n"
            "Likely cause: GPU cold-start / VLM tokenizer init exceeded deadline,\n"
            "or every _step_once call dropped (check policy.select_action logs)."
        )

        # ------------------------------------------------------------------
        # Phase 2: second step should arrive quickly once warmed up
        # ------------------------------------------------------------------
        elapsed = 0.0
        poll = 0.5
        while elapsed < _STEADY_S:
            await asyncio.sleep(poll)
            elapsed += poll
            snapshot = await svc.get(session.id)
            if snapshot.step >= 2:
                break

        assert snapshot.step >= 2, (
            f"Second step not completed within {_STEADY_S:.0f}s after first.\n"
            f"  status={snapshot.status!r}\n"
            f"  last_latency_ms={snapshot.last_latency_ms}"
        )

        assert snapshot.status == "running"
        assert snapshot.last_latency_ms is not None
    finally:
        await svc.stop(session.id)

    final = await svc.get(session.id)
    assert final.status == "stopped"
