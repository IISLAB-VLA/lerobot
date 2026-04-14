"""Benchmark controller tests (Task #16 Phase 1).

The controller is exercised without any real gym rollout — the default
stub runner publishes synthetic step events.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lerobot.dashboard.app import create_app  # noqa: E402
from lerobot.dashboard.core.config import DashboardConfig  # noqa: E402
from lerobot.dashboard.services.benchmark import (  # noqa: E402
    BenchmarkController,
    BenchmarkStartRequest,
    RunNotFoundError,
    UnknownEnvError,
    list_benchmarks,
)


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------


def test_list_benchmarks_surfaces_known_envs() -> None:
    infos = list_benchmarks()
    names = {info.env_name for info in infos}
    # These four are the MVP set declared in lerobot.envs.configs.
    assert {"pusht", "libero", "aloha"} <= names


# ---------------------------------------------------------------------------
# Controller lifecycle
# ---------------------------------------------------------------------------


async def _trivial_runner(run: Any, publish: Any) -> None:
    """Emit two step events then return so the controller finalises the run."""
    for i in range(2):
        await publish(run, {"type": "step", "step": i, "progress": (i + 1) / 2})


async def test_start_run_unknown_env_raises(tmp_path: Path) -> None:
    controller = BenchmarkController(tmp_path)
    with pytest.raises(UnknownEnvError):
        await controller.start_run(
            BenchmarkStartRequest(env_name="not_a_real_env", episodes=1)
        )


async def test_start_run_creates_run_and_storage(tmp_path: Path) -> None:
    controller = BenchmarkController(tmp_path, runner=_trivial_runner)
    summary = await controller.start_run(
        BenchmarkStartRequest(env_name="pusht", episodes=1, seed=42)
    )
    assert summary.run_id.startswith("run-")
    assert summary.env_name == "pusht"
    assert summary.status in {"queued", "running", "completed"}
    assert Path(summary.storage_dir).is_dir()

    # Wait for the run to finish.
    for _ in range(100):
        current = await controller.get_run(summary.run_id)
        if current.status == "completed":
            break
        await asyncio.sleep(0.01)
    assert current.status == "completed"
    assert current.progress == pytest.approx(1.0)


async def test_subscribe_receives_step_and_done(tmp_path: Path) -> None:
    controller = BenchmarkController(tmp_path, runner=_trivial_runner)
    summary = await controller.start_run(
        BenchmarkStartRequest(env_name="pusht", episodes=1)
    )

    collected: list[dict] = []

    async def _consume() -> None:
        async for event in controller.subscribe(summary.run_id):
            collected.append(event)
            if event["type"] == "done":
                return

    await asyncio.wait_for(_consume(), timeout=2.0)
    types = [e["type"] for e in collected]
    assert types[0] == "run"  # replay of current summary
    assert "step" in types
    assert types[-1] == "done"
    assert collected[-1]["status"] == "completed"


async def test_cancel_run_reports_cancelled_state(tmp_path: Path) -> None:
    async def _slow_runner(run: Any, publish: Any) -> None:
        for i in range(1_000):
            if run.cancelled:
                return
            await publish(run, {"type": "step", "step": i, "progress": i / 1000})
            await asyncio.sleep(0.01)

    controller = BenchmarkController(tmp_path, runner=_slow_runner)
    summary = await controller.start_run(
        BenchmarkStartRequest(env_name="pusht", episodes=5)
    )
    await asyncio.sleep(0.05)
    result = await controller.cancel_run(summary.run_id)
    assert result.status == "cancelled"


async def test_cancel_unknown_run_raises(tmp_path: Path) -> None:
    controller = BenchmarkController(tmp_path)
    with pytest.raises(RunNotFoundError):
        await controller.cancel_run("run-nope")


async def test_subscribe_after_completion_yields_terminal(tmp_path: Path) -> None:
    controller = BenchmarkController(tmp_path, runner=_trivial_runner)
    summary = await controller.start_run(
        BenchmarkStartRequest(env_name="pusht", episodes=1)
    )
    # Wait for completion.
    for _ in range(100):
        current = await controller.get_run(summary.run_id)
        if current.status == "completed":
            break
        await asyncio.sleep(0.01)
    events: list[dict] = []
    async for ev in controller.subscribe(summary.run_id):
        events.append(ev)
    # Post-completion subscribe replays run summary + done.
    assert events[0]["type"] == "run"
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "completed"


async def test_list_runs_newest_first(tmp_path: Path) -> None:
    controller = BenchmarkController(tmp_path, runner=_trivial_runner)
    a = await controller.start_run(BenchmarkStartRequest(env_name="pusht", episodes=1))
    await asyncio.sleep(0.01)
    b = await controller.start_run(BenchmarkStartRequest(env_name="pusht", episodes=1))
    runs = await controller.list_runs()
    ids = [r.run_id for r in runs]
    assert ids == [b.run_id, a.run_id]


# ---------------------------------------------------------------------------
# REST contract via TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = DashboardConfig(
        storage_dir=tmp_path / "storage",
        static_dir=None,
        fake_devices=True,
    )
    return TestClient(create_app(config))


def test_rest_list_benchmarks(client: TestClient) -> None:
    resp = client.get("/api/benchmarks")
    assert resp.status_code == 200
    names = {b["env_name"] for b in resp.json()["benchmarks"]}
    assert "pusht" in names


def test_rest_start_run_rejects_unknown_env(client: TestClient) -> None:
    resp = client.post(
        "/api/benchmarks/runs",
        json={"env_name": "not_a_real_env", "episodes": 1},
    )
    assert resp.status_code == 422


def test_rest_start_and_get_run(client: TestClient) -> None:
    start = client.post(
        "/api/benchmarks/runs",
        json={"env_name": "pusht", "episodes": 1},
    )
    assert start.status_code == 202
    body = start.json()
    run_id = body["run_id"]
    assert body["env_name"] == "pusht"

    detail = client.get(f"/api/benchmarks/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id

    # Cancel so the background task doesn't linger past the test.
    cancel = client.post(f"/api/benchmarks/runs/{run_id}/cancel")
    assert cancel.status_code == 202


def test_rest_get_run_404(client: TestClient) -> None:
    resp = client.get("/api/benchmarks/runs/run-nope")
    assert resp.status_code == 404


def test_rest_list_runs_empty_initially(client: TestClient) -> None:
    resp = client.get("/api/benchmarks/runs")
    assert resp.status_code == 200
    assert resp.json() == {"runs": []}
