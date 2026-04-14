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

from lerobot.dashboard.services.benchmark import BENCHMARK_RESOURCE_URL_PREFIX

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover — import deferred until rollout time
    import gymnasium as gym
    import pyarrow as pa

# Schema for trajectory.parquet. Action columns (``action_<key>``) are
# appended dynamically in :class:`TrajectoryWriter` so multi-D action
# spaces flatten cleanly without a column-of-lists. ``obs_jpg_path``
# stores a path *relative to the run's storage_dir* so the parquet
# survives a directory move.
_TRAJECTORY_BASE_FIELDS: tuple[tuple[str, str], ...] = (
    ("run_id", "string"),
    ("policy_slug", "string"),
    ("episode", "int32"),
    ("step", "int32"),
    ("reward", "float32"),
    ("done", "bool"),
    ("truncated", "bool"),
    ("obs_jpg_path", "string"),
)

OBS_JPG_QUALITY = 85
_IMAGE_OBS_KEYS: tuple[str, ...] = ("pixels", "image", "rgb", "observation.image", "observation.images.cam_high")


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
        obs_jpg_path: str | None = None,
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
            obs_jpg_path,
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
        obs_jpg_path: str | None,
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
            "obs_jpg_path": [obs_jpg_path],
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


def _extract_image(obs: Any) -> Any | None:
    """Return an ``(H, W, 3)`` ``uint8`` image or ``None``.

    Tolerates the common gym shapes:

    * dict obs → look for a known image key (``pixels``, ``image``, ``rgb``,
      or any ``observation.image*`` key); fall back to the first ndarray
      with a 3D ``(H, W, 3)`` shape.
    * ndarray obs with shape ``(H, W, 3)`` → use as-is.
    * vector-env ndarray with shape ``(n_envs, H, W, 3)`` → take ``obs[0]``.
    """
    import numpy as np

    if isinstance(obs, dict):
        for key in _IMAGE_OBS_KEYS:
            value = obs.get(key)
            if value is not None:
                value = np.asarray(value)
                value = _maybe_drop_vec_axis(value)
                if _is_image_array(value):
                    return value
        for value in obs.values():
            arr = np.asarray(value)
            arr = _maybe_drop_vec_axis(arr)
            if _is_image_array(arr):
                return arr
        return None
    arr = np.asarray(obs)
    arr = _maybe_drop_vec_axis(arr)
    return arr if _is_image_array(arr) else None


def _maybe_drop_vec_axis(arr: Any) -> Any:
    """Strip a leading ``n_envs`` axis when present (we always run n_envs=1)."""
    if hasattr(arr, "ndim") and arr.ndim == 4 and arr.shape[0] == 1:
        return arr[0]
    return arr


def _is_image_array(arr: Any) -> bool:
    if not hasattr(arr, "shape") or not hasattr(arr, "dtype"):
        return False
    if arr.ndim != 3 or arr.shape[-1] != 3:
        return False
    return arr.dtype.kind in {"u", "i", "f"}


def _coerce_uint8(image: Any) -> Any:
    import numpy as np

    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        if arr.dtype.kind == "f":
            arr = np.clip(arr * 255.0 if arr.max() <= 1.0 else arr, 0, 255)
        arr = arr.astype(np.uint8)
    return arr


def _save_jpeg(image: Any, dest: Path) -> None:
    """Blocking JPG write. Caller wraps in :func:`asyncio.to_thread`."""
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    arr = _coerce_uint8(image)
    Image.fromarray(arr).save(dest, "JPEG", quality=OBS_JPG_QUALITY)


def _encode_episode_to_mp4(jpg_paths: list[Path], dest: Path, fps: int) -> None:
    """Blocking JPG-sequence → H.264 mp4 encode. Wrap in :func:`asyncio.to_thread`."""
    import av
    import numpy as np
    from PIL import Image

    if not jpg_paths:
        raise ValueError("jpg_paths is empty")
    dest.parent.mkdir(parents=True, exist_ok=True)
    first = np.asarray(Image.open(jpg_paths[0]).convert("RGB"))
    height, width, _ = first.shape

    # H.264 needs even dimensions for yuv420p.
    width = width - (width % 2)
    height = height - (height % 2)

    container = av.open(str(dest), mode="w")
    try:
        stream = container.add_stream("h264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.options = {"movflags": "+faststart"}

        for path in jpg_paths:
            arr = np.asarray(Image.open(path).convert("RGB"))[:height, :width]
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():  # flush trailing packets
            container.mux(packet)
    finally:
        container.close()


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

        storage_dir = Path(run.storage_dir)
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

                obs_jpg_path = await self._maybe_save_obs(storage_dir, episode, step, obs)

                await writer.append(
                    policy_slug=self.POLICY_SLUG,
                    episode=episode,
                    step=step,
                    reward=reward_scalar,
                    done=terminated_scalar,
                    truncated=truncated_scalar,
                    action=action_flat,
                    obs_jpg_path=obs_jpg_path,
                )
                run.steps_total = writer.row_count
                run.last_reward = reward_scalar

                step_event: dict[str, Any] = {
                    "type": "step",
                    "episode": episode,
                    "step": step,
                    "reward": reward_scalar,
                    "done": terminated_scalar,
                    "truncated": truncated_scalar,
                    "policy_slug": self.POLICY_SLUG,
                    "progress": (episode + (step + 1) / self._max_steps) / max(run.episodes, 1),
                }
                if obs_jpg_path is not None:
                    step_event["obs_jpg_path"] = obs_jpg_path
                    step_event["obs_jpg_url"] = (
                        f"{BENCHMARK_RESOURCE_URL_PREFIX}/{run.run_id}/{obs_jpg_path}"
                    )
                await publish(run, step_event)

                if terminated_scalar or truncated_scalar:
                    break
            run.progress = (episode + 1) / max(run.episodes, 1)
            await self._finalize_episode_preview(run, episode, publish)

    async def _maybe_save_obs(
        self, storage_dir: Path, episode: int, step: int, obs: Any
    ) -> str | None:
        image = _extract_image(obs)
        if image is None:
            return None
        rel_path = f"observations/episode_{episode}/step_{step}.jpg"
        await asyncio.to_thread(_save_jpeg, image, storage_dir / rel_path)
        return rel_path

    async def _finalize_episode_preview(self, run: Any, episode: int, publish: Any) -> None:
        """Encode the per-episode JPG sequence into an mp4 + publish ``preview_ready``.

        Skips episodes without any saved observations (state-only envs).
        Encoding errors are logged + dropped — they shouldn't kill the run.
        """
        storage_dir = Path(run.storage_dir)
        episode_dir = storage_dir / "observations" / f"episode_{episode}"
        if not episode_dir.is_dir():
            return
        jpg_paths = sorted(episode_dir.glob("step_*.jpg"))
        if not jpg_paths:
            return
        fps = max(int(getattr(run, "fps", 0)) or 10, 1)
        rel_path = f"previews/episode_{episode}.mp4"
        dest = storage_dir / rel_path
        try:
            await asyncio.to_thread(_encode_episode_to_mp4, jpg_paths, dest, fps)
        except Exception:
            logger.exception("preview encoding failed for run %s episode %s", run.run_id, episode)
            return
        await publish(
            run,
            {
                "type": "preview_ready",
                "episode": episode,
                "policy_slug": self.POLICY_SLUG,
                "preview_path": rel_path,
                "preview_url": f"{BENCHMARK_RESOURCE_URL_PREFIX}/{run.run_id}/{rel_path}",
            },
        )

    @staticmethod
    def _seed_for(base_seed: int | None, episode: int) -> int | None:
        if base_seed is None:
            return None
        return int(base_seed) + int(episode)

    @staticmethod
    def _choose_action(vec_env: Any) -> Any:
        return vec_env.action_space.sample()


__all__ = [
    "OBS_JPG_QUALITY",
    "RandomActionEnvRunner",
    "TrajectoryWriter",
]
