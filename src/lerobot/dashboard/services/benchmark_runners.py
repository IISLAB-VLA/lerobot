"""Real-env runners for the benchmark controller.

Task #16 Phase 2a. Two pieces:

* :class:`RandomActionEnvRunner` — drives a real
  :func:`lerobot.envs.factory.make_env` env with random actions sampled
  from ``env.action_space``. Records every step to a
  :class:`TrajectoryWriter`. Phase 2b plugs a real policy in by swapping
  the action source.
* :class:`TrajectoryWriter` — pyarrow-backed streaming parquet writer
  with a stable schema agreed in the RFC. Wraps blocking IO behind
  :func:`asyncio.to_thread` so the async runner stays cooperative.

When the env package is missing (``gym_pusht`` / ``libero`` not installed)
the runner publishes an ``error`` event and lets the controller mark the
run as ``failed``. Phase 2 deliberately does **not** auto-fall-back to
the stub runner — silent fallbacks hide misconfigurations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover — import deferred until rollout time
    import gymnasium as gym
    import pyarrow as pa

# Schema for trajectory.parquet. Action columns (``action_<key>``) are
# appended dynamically in :class:`TrajectoryWriter` so multi-D action
# spaces flatten cleanly without a column-of-lists.
_TRAJECTORY_BASE_FIELDS: tuple[tuple[str, str], ...] = (
    ("run_id", "string"),
    ("policy_slug", "string"),
    ("episode", "int32"),
    ("step", "int32"),
    ("reward", "float32"),
    ("done", "bool"),
    ("truncated", "bool"),
)


# ---------------------------------------------------------------------------
# Trajectory writer
# ---------------------------------------------------------------------------


class TrajectoryWriter:
    """Streaming parquet writer keyed by per-episode RecordBatches.

    The schema is fixed on first write — caller must supply a stable
    list of action component names. Subsequent ``append`` calls reuse
    the same schema. Close on every code path (controller's ``finally``).
    """

    def __init__(self, path: Path, run_id: str, action_keys: list[str]) -> None:
        self.path = path
        self.run_id = run_id
        self.action_keys = list(action_keys)
        self._schema = self._build_schema(self.action_keys)
        self._writer: pa.parquet.ParquetWriter | None = None
        self._row_count = 0

    @staticmethod
    def _build_schema(action_keys: list[str]) -> pa.Schema:
        import pyarrow as pa

        type_map = {
            "string": pa.string(),
            "int32": pa.int32(),
            "float32": pa.float32(),
            "bool": pa.bool_(),
        }
        fields = [pa.field(name, type_map[type_name]) for name, type_name in _TRAJECTORY_BASE_FIELDS]
        fields.extend(pa.field(f"action_{k}", pa.float32()) for k in action_keys)
        return pa.schema(fields)

    def _ensure_open(self) -> None:
        if self._writer is not None:
            return
        import pyarrow.parquet as pq

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = pq.ParquetWriter(str(self.path), self._schema)

    async def append(
        self,
        *,
        policy_slug: str,
        episode: int,
        step: int,
        reward: float,
        done: bool,
        truncated: bool,
        action: list[float],
    ) -> None:
        if len(action) != len(self.action_keys):
            raise ValueError(
                f"action length {len(action)} does not match declared keys "
                f"{self.action_keys}"
            )
        await asyncio.to_thread(
            self._append_sync,
            policy_slug,
            episode,
            step,
            reward,
            done,
            truncated,
            action,
        )

    def _append_sync(
        self,
        policy_slug: str,
        episode: int,
        step: int,
        reward: float,
        done: bool,
        truncated: bool,
        action: list[float],
    ) -> None:
        import pyarrow as pa

        self._ensure_open()
        assert self._writer is not None
        columns: dict[str, list[Any]] = {
            "run_id": [self.run_id],
            "policy_slug": [policy_slug],
            "episode": [episode],
            "step": [step],
            "reward": [float(reward)],
            "done": [bool(done)],
            "truncated": [bool(truncated)],
        }
        for key, value in zip(self.action_keys, action, strict=True):
            columns[f"action_{key}"] = [float(value)]
        batch = pa.record_batch(columns, schema=self._schema)
        self._writer.write_batch(batch)
        self._row_count += 1

    async def close(self) -> None:
        if self._writer is None:
            return
        await asyncio.to_thread(self._writer.close)
        self._writer = None

    @property
    def row_count(self) -> int:
        return self._row_count


# ---------------------------------------------------------------------------
# Random-action runner
# ---------------------------------------------------------------------------


EnvFactory = Callable[[Any], "dict[str, dict[int, gym.vector.VectorEnv]]"]


def _default_env_factory(env_name: str) -> dict[str, Any]:
    """Default factory: build the env via :func:`lerobot.envs.factory.make_env`.

    Lazy-imports the factory so the dashboard module stays importable
    without any gym env package installed.
    """
    from lerobot.envs.configs import EnvConfig
    from lerobot.envs.factory import make_env

    cls = EnvConfig.get_choice_class(env_name)
    cfg = cls()
    return make_env(cfg, n_envs=1)


def _action_keys_from_space(space: Any, prefix: str = "a") -> list[str]:
    """Generate stable action column names from a gym action space.

    For a ``Box`` space we flatten using ``a0`` / ``a1`` / ... so the
    parquet schema is fixed even when the space is multidimensional.
    """
    import numpy as np

    if hasattr(space, "shape") and space.shape is not None:
        size = int(np.prod(space.shape))
        return [f"{prefix}{i}" for i in range(size)]
    raise ValueError(f"unsupported action space: {space!r}")


class RandomActionEnvRunner:
    """Phase 2a runner: real env via factory, random action per step.

    Records every transition to a :class:`TrajectoryWriter` and publishes
    ``step`` events to the controller's WS bus. Phase 2b replaces
    ``_choose_action`` with a real policy.
    """

    POLICY_SLUG = "random"

    def __init__(
        self,
        env_factory: EnvFactory | None = None,
        max_steps_per_episode: int = 500,
    ) -> None:
        self._env_factory = env_factory or _default_env_factory
        self._max_steps = max_steps_per_episode

    async def __call__(self, run: Any, publish: Any) -> None:
        try:
            envs = await asyncio.to_thread(self._env_factory, run.env_name)
        except ImportError as exc:
            message = (
                f"env package for {run.env_name!r} not installed. "
                f"Install via `uv sync --extra benchmarks` or the env-specific extra."
            )
            logger.warning(message + f" ({exc})")
            await publish(run, {"type": "error", "code": "EnvPackageMissing", "message": message})
            raise RuntimeError(message) from exc
        except Exception as exc:
            message = f"failed to build env {run.env_name!r}: {exc}"
            logger.exception(message)
            await publish(run, {"type": "error", "code": exc.__class__.__name__, "message": message})
            raise

        # Pick the first suite/task — Phase 4 supports multi-task.
        suite_name = next(iter(envs))
        vec_env = next(iter(envs[suite_name].values()))
        action_keys = _action_keys_from_space(vec_env.single_action_space)

        writer = TrajectoryWriter(
            path=Path(run.storage_dir) / "trajectory.parquet",
            run_id=run.run_id,
            action_keys=action_keys,
        )

        try:
            await self._rollout(run, vec_env, writer, publish)
        finally:
            await writer.close()
            await asyncio.to_thread(vec_env.close)

    async def _rollout(self, run: Any, vec_env: Any, writer: TrajectoryWriter, publish: Any) -> None:
        import numpy as np

        for episode in range(run.episodes):
            if run.cancelled:
                return
            seed = self._seed_for(run.seed, episode)
            await asyncio.to_thread(vec_env.reset, seed=seed)
            run.current_episode = episode

            for step in range(self._max_steps):
                if run.cancelled:
                    return
                action = await asyncio.to_thread(self._choose_action, vec_env)
                obs, reward, terminated, truncated, info = await asyncio.to_thread(vec_env.step, action)

                # vec_env returns arrays of shape (n_envs,) — n_envs == 1 here.
                reward_scalar = float(np.asarray(reward).flatten()[0])
                terminated_scalar = bool(np.asarray(terminated).flatten()[0])
                truncated_scalar = bool(np.asarray(truncated).flatten()[0])
                action_flat = np.asarray(action).flatten().tolist()

                await writer.append(
                    policy_slug=self.POLICY_SLUG,
                    episode=episode,
                    step=step,
                    reward=reward_scalar,
                    done=terminated_scalar,
                    truncated=truncated_scalar,
                    action=action_flat,
                )
                run.steps_total = writer.row_count
                run.last_reward = reward_scalar

                await publish(
                    run,
                    {
                        "type": "step",
                        "episode": episode,
                        "step": step,
                        "reward": reward_scalar,
                        "done": terminated_scalar,
                        "truncated": truncated_scalar,
                        "policy_slug": self.POLICY_SLUG,
                        "progress": (episode + (step + 1) / self._max_steps) / max(run.episodes, 1),
                    },
                )

                if terminated_scalar or truncated_scalar:
                    break
            run.progress = (episode + 1) / max(run.episodes, 1)

    @staticmethod
    def _seed_for(base_seed: int | None, episode: int) -> int | None:
        if base_seed is None:
            return None
        return int(base_seed) + int(episode)

    @staticmethod
    def _choose_action(vec_env: Any) -> Any:
        return vec_env.action_space.sample()


__all__ = [
    "RandomActionEnvRunner",
    "TrajectoryWriter",
]
