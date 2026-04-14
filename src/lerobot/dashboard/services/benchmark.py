"""Benchmark run controller.

Task #16 Phase 1 scaffolding. Matches the calibration-controller pattern
(REST-authoritative state machine + WS event stream + per-session
background task for streaming) but tracks benchmark *runs* keyed by
``run_id`` rather than a per-robot singleton.

Phase 1 (this commit):
* ``list_benchmarks()`` enumerates :class:`lerobot.envs.configs.EnvConfig`
  choice registry (pusht / libero / aloha / gym_manipulator / ...).
* :class:`BenchmarkController` tracks runs, starts a stub worker that
  emits synthetic ``step`` events at the env's declared ``fps`` and a
  terminal ``done`` event. No gym rollout yet — the worker is replaced
  in Phase 2 by a real :func:`lerobot.envs.factory.make_env` + policy
  loop.
* Storage dir is allocated per run (``{storage}/benchmarks/{run_id}``)
  but left empty until Phase 2 writes parquet + preview frames.

The stub runner is intentionally boring so the contract can be pinned
down with frontend-architect-3 before the real rollout lands; it also
lets the FE page consume WS events without hardware.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from lerobot.envs.configs import EnvConfig

logger = logging.getLogger(__name__)

_TERMINAL = object()

# Stub runner publish rate cap. Real runs use the env's declared fps.
_STUB_STEPS_PER_EPISODE = 25
_STUB_MAX_HZ = 30.0


RunStatus = Literal["queued", "running", "completed", "cancelled", "failed"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BenchmarkError(RuntimeError):
    """Base class for benchmark controller errors."""


class UnknownEnvError(BenchmarkError):
    """``env_name`` doesn't match a registered :class:`EnvConfig` subclass."""


class RunNotFoundError(BenchmarkError):
    """``run_id`` doesn't correspond to a tracked run."""


# ---------------------------------------------------------------------------
# Wire-format pydantic models
# ---------------------------------------------------------------------------


class BenchmarkInfo(BaseModel):
    """One entry in the ``list_benchmarks`` response."""

    model_config = ConfigDict(extra="forbid")

    env_name: str
    task: str | None = None
    fps: int = Field(ge=1)
    config_type: str


class BenchmarkStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env_name: str
    policy_refs: list[str] = Field(default_factory=list)
    episodes: int = Field(default=3, ge=1, le=1000)
    seed: int | None = None
    task: str | None = None


class BenchmarkRunSummary(BaseModel):
    """Point-in-time view of a run. Returned from REST + embedded in WS events."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    env_name: str
    task: str | None = None
    policy_refs: list[str]
    episodes: int
    seed: int | None = None
    status: RunStatus
    progress: float = Field(ge=0.0, le=1.0)
    current_episode: int = Field(ge=0)
    steps_total: int = Field(ge=0)
    last_reward: float | None = None
    started_at: datetime
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    storage_dir: str


class BenchmarkRunList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: list[BenchmarkRunSummary]


# ---------------------------------------------------------------------------
# Env registry helper
# ---------------------------------------------------------------------------


def list_benchmarks() -> list[BenchmarkInfo]:
    """Enumerate :class:`EnvConfig` subclasses registered via draccus.

    Each choice is instantiated with the subclass defaults so we can read
    its declared ``fps`` / ``task`` metadata; subclasses that require
    mandatory constructor args (rare) are skipped with a warning.
    """
    infos: list[BenchmarkInfo] = []
    for name, cls in EnvConfig.get_known_choices().items():
        try:
            cfg = cls()
        except TypeError as exc:
            logger.debug("skipping env %s — requires constructor args: %s", name, exc)
            continue
        infos.append(
            BenchmarkInfo(
                env_name=name,
                task=cfg.task,
                fps=int(cfg.fps),
                config_type=cls.__name__,
            )
        )
    infos.sort(key=lambda b: b.env_name)
    return infos


# ---------------------------------------------------------------------------
# Internal run state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Run:
    run_id: str
    env_name: str
    task: str | None
    policy_refs: list[str]
    episodes: int
    seed: int | None
    fps: int
    storage_dir: Path
    status: RunStatus = "queued"
    progress: float = 0.0
    current_episode: int = 0
    steps_total: int = 0
    last_reward: float | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    subscribers: list[asyncio.Queue[Any]] = field(default_factory=list)
    cancelled: bool = False
    task_handle: asyncio.Task[None] | None = None

    def to_summary(self) -> BenchmarkRunSummary:
        return BenchmarkRunSummary(
            run_id=self.run_id,
            env_name=self.env_name,
            task=self.task,
            policy_refs=list(self.policy_refs),
            episodes=self.episodes,
            seed=self.seed,
            status=self.status,
            progress=self.progress,
            current_episode=self.current_episode,
            steps_total=self.steps_total,
            last_reward=self.last_reward,
            started_at=self.started_at,
            completed_at=self.completed_at,
            result=self.result,
            error=self.error,
            storage_dir=str(self.storage_dir),
        )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class BenchmarkController:
    """Tracks active + historical benchmark runs.

    Parameters
    ----------
    storage_dir:
        Parent directory. One subdirectory per run at ``{storage_dir}/{run_id}/``.
    runner:
        Async callable ``(run, publish) -> None`` that executes the benchmark.
        Defaults to :meth:`_default_stub_runner`. Phase 2 injects a real
        gym rollout runner here.
    """

    def __init__(
        self,
        storage_dir: Path,
        runner: Any | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[str, _Run] = {}
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._runner = runner or self._default_stub_runner

    # ----- listing --------------------------------------------------------

    @staticmethod
    def list_benchmarks() -> list[BenchmarkInfo]:
        return list_benchmarks()

    # ----- run lifecycle --------------------------------------------------

    async def start_run(self, body: BenchmarkStartRequest) -> BenchmarkRunSummary:
        known = {info.env_name: info for info in list_benchmarks()}
        info = known.get(body.env_name)
        if info is None:
            raise UnknownEnvError(
                f"unknown env {body.env_name!r}; known: {sorted(known)}"
            )
        run_id = f"run-{secrets.token_hex(6)}"
        storage = self._storage_dir / run_id
        storage.mkdir(parents=True, exist_ok=True)
        run = _Run(
            run_id=run_id,
            env_name=body.env_name,
            task=body.task or info.task,
            policy_refs=list(body.policy_refs),
            episodes=body.episodes,
            seed=body.seed,
            fps=info.fps,
            storage_dir=storage,
        )
        async with self._lock:
            self._runs[run_id] = run

        run.task_handle = asyncio.create_task(
            self._drive_run(run),
            name=f"benchmark-run-{run_id}",
        )
        return run.to_summary()

    async def list_runs(self) -> list[BenchmarkRunSummary]:
        async with self._lock:
            runs = list(self._runs.values())
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return [r.to_summary() for r in runs]

    async def get_run(self, run_id: str) -> BenchmarkRunSummary:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise RunNotFoundError(f"unknown run {run_id!r}")
            return run.to_summary()

    async def cancel_run(self, run_id: str) -> BenchmarkRunSummary:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise RunNotFoundError(f"unknown run {run_id!r}")
            if run.status in {"completed", "cancelled", "failed"}:
                return run.to_summary()
            run.cancelled = True
        if run.task_handle is not None:
            run.task_handle.cancel()
            try:
                await run.task_handle
            except (asyncio.CancelledError, Exception):
                pass
        return run.to_summary()

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise RunNotFoundError(f"unknown run {run_id!r}")
            # Replay the current summary so late subscribers have state immediately.
            await queue.put({"type": "run", "summary": run.to_summary().model_dump()})
            if run.status in {"completed", "cancelled", "failed"}:
                # Already terminal — send the terminal event then close.
                _enqueue_drop_oldest(queue, _terminal_event(run))
                _enqueue_drop_oldest(queue, _TERMINAL)
            else:
                run.subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is _TERMINAL:
                    return
                yield item
        finally:
            async with self._lock:
                run = self._runs.get(run_id)
                if run is not None:
                    try:
                        run.subscribers.remove(queue)
                    except ValueError:
                        pass

    # ----- runner ---------------------------------------------------------

    async def _drive_run(self, run: _Run) -> None:
        try:
            async with self._lock:
                run.status = "running"
                self._publish_run_locked(run)

            await self._runner(run, self._publish_step)

            async with self._lock:
                if run.cancelled:
                    run.status = "cancelled"
                else:
                    run.status = "completed"
                    run.progress = 1.0
                    run.result = {"episodes_completed": run.current_episode}
        except asyncio.CancelledError:
            async with self._lock:
                run.status = "cancelled"
        except Exception as exc:
            logger.exception("benchmark run %s failed", run.run_id)
            async with self._lock:
                run.status = "failed"
                run.error = {"code": exc.__class__.__name__, "message": str(exc) or "unknown"}
        finally:
            async with self._lock:
                run.completed_at = datetime.now(UTC)
                self._publish_run_locked(run)
                for queue in list(run.subscribers):
                    _enqueue_drop_oldest(queue, _terminal_event(run))
                    _enqueue_drop_oldest(queue, _TERMINAL)
                run.subscribers.clear()

    async def _default_stub_runner(
        self,
        run: _Run,
        publish_step: Any,
    ) -> None:
        """Synthetic rollout for Phase 1.

        Emits ``_STUB_STEPS_PER_EPISODE`` steps per episode at ``run.fps``
        (capped at :data:`_STUB_MAX_HZ` so CI stays fast), with reward
        proportional to step progress. Each tick awaits a small sleep so
        the event loop stays responsive and cancellation lands quickly.
        """
        interval = 1.0 / min(max(run.fps, 1), _STUB_MAX_HZ)
        total_steps = run.episodes * _STUB_STEPS_PER_EPISODE
        step_counter = 0
        for episode in range(run.episodes):
            async with self._lock:
                if run.cancelled:
                    return
                run.current_episode = episode
            for step in range(_STUB_STEPS_PER_EPISODE):
                async with self._lock:
                    if run.cancelled:
                        return
                step_counter += 1
                reward = step / max(_STUB_STEPS_PER_EPISODE - 1, 1)
                async with self._lock:
                    run.steps_total = step_counter
                    run.progress = step_counter / total_steps
                    run.last_reward = reward
                await publish_step(
                    run,
                    {
                        "type": "step",
                        "episode": episode,
                        "step": step,
                        "reward": reward,
                        "done": step == _STUB_STEPS_PER_EPISODE - 1,
                        "progress": step_counter / total_steps,
                    },
                )
                await asyncio.sleep(interval)

    # ----- publish helpers ------------------------------------------------

    async def _publish_step(self, run: _Run, event: dict[str, Any]) -> None:
        async with self._lock:
            for queue in list(run.subscribers):
                _enqueue_drop_oldest(queue, event)

    def _publish_run_locked(self, run: _Run) -> None:
        event = {"type": "run", "summary": run.to_summary().model_dump()}
        for queue in list(run.subscribers):
            _enqueue_drop_oldest(queue, event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _terminal_event(run: _Run) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "done",
        "run_id": run.run_id,
        "status": run.status,
    }
    if run.error is not None:
        event["error"] = run.error
    if run.result is not None:
        event["result"] = run.result
    return event


def _enqueue_drop_oldest(queue: asyncio.Queue[Any], item: Any) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:  # pragma: no cover
        pass


__all__ = [
    "BenchmarkController",
    "BenchmarkError",
    "BenchmarkInfo",
    "BenchmarkRunList",
    "BenchmarkRunSummary",
    "BenchmarkStartRequest",
    "RunNotFoundError",
    "RunStatus",
    "UnknownEnvError",
    "list_benchmarks",
]


# UUID import kept at the top for the _Run.run_id alias usage-check.
_ = uuid4
