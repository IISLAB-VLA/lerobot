"""Live VLA inference service for the dashboard (task #14).

Sits between the existing robot/camera managers and a loaded
:class:`~lerobot.policies.pretrained.PreTrainedPolicy`. Each session owns
one capture+inference loop that dual-pulls observation from the robot
manager and camera frames from the camera manager (same pattern as
:mod:`recorder`), runs the policy, and optionally calls
``robot_manager.send_action`` to close the loop.

Design
------
* **One session per robot**. Prevents two policies fighting over the
  same hardware or racing on VRAM.
* **Serialized policy.select_action**. A shared asyncio.Lock on the
  service wraps every policy forward pass so concurrent sessions sharing
  the same CUDA device don't explode VRAM.
* **LRU policy cache**. ``load_policy`` holds at most ``max_resident``
  policies in memory; least-recently-used are dropped on overflow.
* **dry_run**. When enabled the loop still reads obs + runs the policy
  but never calls send_action — the operator can observe behaviour
  safely before flipping the switch.
* **Command swap**. ``set_command`` updates the session's
  ``task_description`` between ticks so language-conditioned policies
  can be re-steered without a restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.services.camera_manager import CameraManagerProtocol
from lerobot.dashboard.services.registry import Registry
from lerobot.dashboard.services.registry_models import CameraEntry, RobotEntry
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol

logger = logging.getLogger(__name__)


class InferenceError(Exception):
    """Base class for inference errors surfaced to the API layer."""


class InferenceNotFoundError(InferenceError):
    """Session id not found."""


class InferenceConflictError(InferenceError):
    """Start refused (duplicate per robot) or stop refused (terminal)."""


class InferenceValidationError(InferenceError):
    """Inputs failed validation (unknown repo_id, robot offline, ...)."""


InferenceStatus = Literal["starting", "running", "stopping", "stopped", "failed"]
_TERMINAL_STATUSES: frozenset[str] = frozenset({"stopped", "failed"})
_LANGUAGE_POLICY_TYPES: frozenset[str] = frozenset({"pi0", "pi05", "smolvla", "wall_x"})
_REPO_ID_RE = re.compile(r"^[\w.\-]+/[\w.\-]+$|^[\w.\-]+$")


# ---------------------------------------------------------------------------
# Policy discovery + loading
# ---------------------------------------------------------------------------


class PolicyDescriptor(BaseModel):
    """Summary of a locally cached policy checkpoint."""

    model_config = ConfigDict(extra="forbid")

    repo_id: str
    policy_type: str
    root: str
    num_parameters: int | None = None
    last_modified: datetime | None = None
    observation_features: dict[str, Any] | None = None
    action_features: dict[str, Any] | None = None
    supports_language: bool = False


class StartRequest(BaseModel):
    """Payload accepted by :meth:`InferenceService.start`.

    ``max_action_magnitude`` and ``deadman_required`` are per-session
    safety knobs layered on top of adapter-level clamps (e.g. UR's
    ``max_relative_target``). The adapter limits stay in place as the
    hardware-protection floor; these knobs are the operator's "policy
    operating envelope" that can be tightened per run.
    """

    model_config = ConfigDict(extra="forbid")

    robot_id: UUID
    repo_id: str = Field(..., min_length=1, max_length=200)
    fps: int = Field(..., ge=1, le=240)
    task_description: str = Field(default="", max_length=500)
    dry_run: bool = False
    max_action_magnitude: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Absolute cap applied to each element of the policy's action "
            "vector before ``send_action``. ``None`` means no session-level cap "
            "(adapter safety clamps still apply)."
        ),
    )
    deadman_required: bool = Field(
        default=False,
        description=(
            "When True the loop skips ``send_action`` unless the operator "
            "is actively holding the deadman (see ``set_deadman``). Dry-run "
            "sessions ignore this field."
        ),
    )


class InferenceSession(BaseModel):
    """Public view of an inference session for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    robot_id: UUID
    repo_id: str
    fps: int
    dry_run: bool
    task_description: str
    status: InferenceStatus
    step: int = 0
    last_latency_ms: float | None = None
    started_at: datetime
    stopped_at: datetime | None = None
    error: str | None = None
    max_action_magnitude: float | None = None
    deadman_required: bool = False
    deadman_held: bool = False
    suppressed_steps: int = 0


class StepEvent(BaseModel):
    """One inference tick pushed to WS subscribers."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["step", "stopped", "error"] = "step"
    step: int = 0
    action: list[float] = Field(default_factory=list)
    latency_ms: float = 0.0
    image_hash: str | None = None
    text: str | None = None
    message: str | None = None
    reason: str | None = None


PolicyLoader = Callable[[str], Any]
"""Callable ``repo_id -> PreTrainedPolicy``. Injectable for tests."""


def _default_policy_loader(repo_id: str) -> Any:  # pragma: no cover — touches HF cache
    """Thread-safe policy loader that resolves the concrete class from the cache.

    ``PreTrainedPolicy.from_pretrained`` cannot be called on the abstract
    base class directly — it would try to instantiate the abstract class.
    Instead we:
    1. Load the config (draccus parses ``config.json`` and returns the
       concrete sub-config, e.g. ``SmolVLAConfig``).
    2. Resolve the concrete policy class via ``get_policy_class(config.type)``.
    3. Load weights via ``cls.from_pretrained(repo_id, config=config)``.
    """
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.policies.factory import get_policy_class

    config = PreTrainedConfig.from_pretrained(repo_id)
    cls = get_policy_class(config.type)
    return cls.from_pretrained(repo_id, config=config)


def scan_hf_cache(cache_dir: Path | None = None) -> list[PolicyDescriptor]:
    """Enumerate locally cached LeRobot policies.

    Scans the HuggingFace cache directory (``cache_dir``, defaulting to
    ``HF_HOME/hub``) for repos whose ``config.json`` declares a
    ``policy_type`` known to :func:`lerobot.policies.factory.get_policy_class`.
    Unknown or partially-downloaded repos are skipped with a debug log
    rather than raising — a missing policy file shouldn't block the UI
    from listing the rest.
    """
    from huggingface_hub import scan_cache_dir

    try:
        info = scan_cache_dir(cache_dir) if cache_dir is not None else scan_cache_dir()
    except Exception as exc:  # noqa: BLE001 — a bad cache should not break list endpoints
        logger.warning("scan_cache_dir failed: %s", exc)
        return []

    descriptors: list[PolicyDescriptor] = []
    for repo in info.repos:
        if repo.repo_type != "model":
            continue
        descriptor = _describe_cached_repo(repo)
        if descriptor is not None:
            descriptors.append(descriptor)
    descriptors.sort(key=lambda d: (d.last_modified or datetime.min), reverse=True)
    return descriptors


def _describe_cached_repo(repo: Any) -> PolicyDescriptor | None:
    """Build a :class:`PolicyDescriptor` from a single ``CachedRepoInfo``."""
    import json

    repo_path = Path(repo.repo_path)
    config_path = _find_first(repo_path, "config.json")
    if config_path is None:
        return None
    try:
        with config_path.open(encoding="utf-8") as fp:
            config = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("skip cached repo %s (unreadable config.json): %s", repo.repo_id, exc)
        return None
    policy_type = config.get("policy_type") or config.get("type")
    if not policy_type:
        return None
    if not _is_known_policy_type(policy_type):
        return None
    last_modified = datetime.fromtimestamp(repo.last_modified, tz=UTC) if repo.last_modified else None
    return PolicyDescriptor(
        repo_id=repo.repo_id,
        policy_type=policy_type,
        root=str(repo_path),
        num_parameters=config.get("num_parameters"),
        last_modified=last_modified,
        observation_features=config.get("input_features") or config.get("observation_features"),
        action_features=config.get("output_features") or config.get("action_features"),
        supports_language=policy_type in _LANGUAGE_POLICY_TYPES,
    )


def _find_first(root: Path, filename: str) -> Path | None:
    """Return the first ``filename`` under ``root`` (breadth-first)."""
    for candidate in root.rglob(filename):
        if candidate.is_file():
            return candidate
    return None


def _is_known_policy_type(policy_type: str) -> bool:
    try:
        from lerobot.policies.factory import get_policy_class

        get_policy_class(policy_type)
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# LRU policy cache
# ---------------------------------------------------------------------------


class PolicyCache:
    """Least-recently-used cache of loaded :class:`PreTrainedPolicy` handles.

    Default ``max_resident=2`` keeps typical dashboard memory bounded when
    the operator toggles between a small set of policies without forcing
    a weight reload on every start/stop.
    """

    def __init__(
        self,
        loader: PolicyLoader,
        max_resident: int = 2,
    ) -> None:
        self._loader = loader
        self._max = max_resident
        self._lock = asyncio.Lock()
        self._entries: OrderedDict[str, Any] = OrderedDict()

    async def get(self, repo_id: str) -> Any:
        async with self._lock:
            if repo_id in self._entries:
                self._entries.move_to_end(repo_id)
                return self._entries[repo_id]
        # Load outside the lock — a cold model can take seconds and we
        # don't want to block concurrent ``get`` calls for other repos.
        policy = await asyncio.to_thread(self._loader, repo_id)
        async with self._lock:
            self._entries[repo_id] = policy
            self._entries.move_to_end(repo_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return policy

    async def evict(self, repo_id: str) -> None:
        async with self._lock:
            self._entries.pop(repo_id, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Session:
    model: InferenceSession
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set[asyncio.Queue[StepEvent]] = field(default_factory=set)
    robot_entry: RobotEntry | None = None
    cameras: list[CameraEntry] = field(default_factory=list)
    camera_iters: dict[UUID, AsyncIterator[Any]] = field(default_factory=dict)
    policy: Any = None
    preprocessor: Any = None  # PolicyProcessorPipeline | None
    postprocessor: Any = None  # PolicyProcessorPipeline | None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class InferenceService:
    """Orchestrates inference sessions + the capture/inference loop."""

    def __init__(
        self,
        registry: Registry,
        robot_manager: RobotManagerProtocol,
        camera_manager: CameraManagerProtocol,
        policy_loader: PolicyLoader | None = None,
        cache_dir: Path | None = None,
        cache_ttl_s: float = 60.0,
    ) -> None:
        self._registry = registry
        self._robot_manager = robot_manager
        self._camera_manager = camera_manager
        self._cache_dir = cache_dir
        self._cache_ttl_s = cache_ttl_s
        self._policy_cache = PolicyCache(policy_loader or _default_policy_loader)
        # Global lock serialising every policy.select_action call across
        # sessions so a shared CUDA device doesn't OOM.
        self._policy_exec_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._sessions: dict[UUID, _Session] = {}
        self._descriptor_cache: tuple[float, list[PolicyDescriptor]] | None = None

    # ------------------------------------------------------------------
    # Policy discovery
    # ------------------------------------------------------------------

    async def list_policies(self, *, refresh: bool = False) -> list[PolicyDescriptor]:
        """Return cached descriptors, refreshing once per ``cache_ttl_s``."""
        now = time.monotonic()
        if not refresh and self._descriptor_cache is not None:
            cached_at, cached = self._descriptor_cache
            if now - cached_at < self._cache_ttl_s:
                return list(cached)
        descriptors = await asyncio.to_thread(scan_hf_cache, self._cache_dir)
        self._descriptor_cache = (now, descriptors)
        return list(descriptors)

    async def get_policy(self, repo_id: str) -> PolicyDescriptor:
        for descriptor in await self.list_policies():
            if descriptor.repo_id == repo_id:
                return descriptor
        raise InferenceNotFoundError(f"policy {repo_id} not cached locally")

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start(self, req: StartRequest) -> InferenceSession:
        if not _REPO_ID_RE.match(req.repo_id):
            raise InferenceValidationError("repo_id must be a valid ``user/model`` or ``model`` identifier")
        robot = await self._registry.get_robot(req.robot_id)
        all_cameras = await self._registry.list_cameras()
        cam_map = {c.id: c for c in all_cameras}
        cameras = [cam_map[cid] for cid in robot.cameras if cid in cam_map]

        if not await self._robot_manager.is_connected(req.robot_id):
            raise InferenceValidationError(
                f"robot {req.robot_id} is not connected; /connect before inference"
            )
        # Refuse up-front if the repo isn't in the cache, so the user gets
        # a fast 404 instead of a minute-long HF download attempt.
        descriptors = {d.repo_id for d in await self.list_policies()}
        if req.repo_id not in descriptors:
            raise InferenceNotFoundError(f"policy {req.repo_id} not cached locally")

        async with self._lock:
            active = next(
                (
                    s
                    for s in self._sessions.values()
                    if s.model.robot_id == req.robot_id and s.model.status not in _TERMINAL_STATUSES
                ),
                None,
            )
            if active is not None:
                raise InferenceConflictError(
                    f"robot {req.robot_id} already has an active inference (session {active.model.id})"
                )
            model = InferenceSession(
                id=uuid4(),
                robot_id=req.robot_id,
                repo_id=req.repo_id,
                fps=req.fps,
                dry_run=req.dry_run,
                task_description=req.task_description,
                status="starting",
                started_at=datetime.now(UTC),
                max_action_magnitude=req.max_action_magnitude,
                deadman_required=req.deadman_required,
            )
            session = _Session(model=model, robot_entry=robot, cameras=cameras)
            self._sessions[model.id] = session

        try:
            session.policy = await self._policy_cache.get(req.repo_id)
            self._validate_feature_compatibility(
                session.policy,
                await self._robot_manager.get_features(req.robot_id),
            )
            await self._open_camera_subscribers(session)
            # Load pre/post-processor pipelines so real policies (e.g. smolvla)
            # receive properly converted tensors + language tokens at each step.
            # Falls back to (None, None) for stubs and policies without saved
            # processor configs — those use the direct select_action path.
            session.preprocessor, session.postprocessor = await self._load_processors(
                req.repo_id, session.policy
            )
        except InferenceError:
            # Typed errors (validation / conflict / not-found) already map
            # to the right HTTP status — propagate them without wrapping,
            # and forget the half-initialised session so the robot id is
            # free for a corrected retry.
            await self._close_camera_subscribers(session)
            async with self._lock:
                self._sessions.pop(session.model.id, None)
            raise
        except Exception as exc:
            await self._close_camera_subscribers(session)
            async with session.lock:
                session.model = session.model.model_copy(update={"status": "failed", "error": str(exc)})
            logger.exception("inference: policy load failed for %s", req.repo_id)
            raise InferenceError(f"policy init failed: {exc}") from exc

        session.task = asyncio.create_task(self._run(session), name=f"inference:{model.id}")
        async with session.lock:
            session.model = session.model.model_copy(update={"status": "running"})
        return session.model.model_copy()

    async def stop(self, session_id: UUID, *, reason: str = "client") -> InferenceSession:
        session = self._require(session_id)
        async with session.lock:
            if session.model.status in _TERMINAL_STATUSES:
                raise InferenceConflictError(f"session {session_id} is already {session.model.status}")
            if session.model.status == "stopping":
                raise InferenceConflictError(f"session {session_id} is already stopping")
            session.model = session.model.model_copy(update={"status": "stopping"})

        if session.task is not None:
            session.task.cancel()
            with suppress(asyncio.CancelledError):
                await session.task

        await self._close_camera_subscribers(session)
        async with session.lock:
            session.model = session.model.model_copy(
                update={"status": "stopped", "stopped_at": datetime.now(UTC)}
            )
        await self._emit(
            session,
            StepEvent(
                type="stopped",
                step=session.model.step,
                reason=reason,
            ),
        )
        return session.model.model_copy()

    async def set_deadman(self, session_id: UUID, held: bool) -> InferenceSession:
        """Arm or release the session's deadman gate.

        ``deadman_required=True`` sessions only forward ``send_action`` while
        ``held=True``. Calling this on a session that didn't opt into the
        gate is a no-op (except for recording the current state).
        """
        session = self._require(session_id)
        async with session.lock:
            if session.model.status in _TERMINAL_STATUSES:
                raise InferenceConflictError(f"session {session_id} is already {session.model.status}")
            session.model = session.model.model_copy(update={"deadman_held": bool(held)})
            return session.model.model_copy()

    async def set_command(self, session_id: UUID, text: str) -> InferenceSession:
        session = self._require(session_id)
        async with session.lock:
            if session.model.status in _TERMINAL_STATUSES:
                raise InferenceConflictError(f"session {session_id} is already {session.model.status}")
            session.model = session.model.model_copy(update={"task_description": text})
            return session.model.model_copy()

    async def list(self) -> list[InferenceSession]:
        async with self._lock:
            return [s.model.model_copy() for s in self._sessions.values()]

    async def get(self, session_id: UUID) -> InferenceSession:
        return self._require(session_id).model.model_copy()

    async def subscribe(self, session_id: UUID) -> AsyncIterator[StepEvent]:
        session = self._require(session_id)
        queue: asyncio.Queue[StepEvent] = asyncio.Queue(maxsize=128)
        async with session.lock:
            session.subscribers.add(queue)
            terminal = session.model.status in _TERMINAL_STATUSES
        try:
            if terminal:
                yield StepEvent(
                    type="stopped" if session.model.status == "stopped" else "error",
                    step=session.model.step,
                    message=session.model.error,
                )
                return
            while True:
                event = await queue.get()
                yield event
                if event.type in {"stopped", "error"}:
                    return
        finally:
            async with session.lock:
                session.subscribers.discard(queue)

    async def close(self) -> None:
        async with self._lock:
            active = [s for s in self._sessions.values() if s.model.status not in _TERMINAL_STATUSES]
        for session in active:
            with suppress(Exception):
                await self.stop(session.model.id, reason="shutdown")
        await self._policy_cache.clear()

    # ------------------------------------------------------------------
    # Capture + inference loop
    # ------------------------------------------------------------------

    async def _run(self, session: _Session) -> None:
        period = 1.0 / session.model.fps
        try:
            while True:
                tick = time.monotonic()
                dropped = await self._step_once(session)
                if not dropped:
                    async with session.lock:
                        session.model = session.model.model_copy(update={"step": session.model.step + 1})
                elapsed = time.monotonic() - tick
                await asyncio.sleep(max(0.0, period - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with session.lock:
                session.model = session.model.model_copy(update={"status": "failed", "error": str(exc)})
            await self._emit(session, StepEvent(type="error", message=str(exc)))
            logger.exception("inference: loop crashed for %s", session.model.id)

    async def _step_once(self, session: _Session) -> bool:
        """One observation + policy.select_action + optional send_action."""
        try:
            obs_raw = await self._robot_manager.read_observation(session.model.robot_id)
        except Exception as exc:
            logger.warning("read_observation failed: %s", exc)
            return True

        # Strip metadata keys that are not numpy arrays (e.g. ``robot_id``,
        # ``online``, ``timestamp`` produced by InMemoryRobotManager).
        # ``prepare_observation_for_inference`` calls ``torch.from_numpy`` on
        # every value, so non-array entries would raise a TypeError.
        obs: dict[str, np.ndarray] = {k: v for k, v in obs_raw.items() if isinstance(v, np.ndarray)}

        stall_timeout = max((1.0 / session.model.fps) * 3, 0.25)
        images: dict[str, np.ndarray] = {}
        for cam in session.cameras:
            iterator = session.camera_iters.get(cam.id)
            if iterator is None:
                continue
            try:
                image = await asyncio.wait_for(anext(iterator), timeout=stall_timeout)
            except (TimeoutError, StopAsyncIteration) as exc:
                logger.warning("camera %s subscribe stalled: %s", cam.id, exc)
                return True
            except Exception as exc:
                logger.warning("camera %s subscribe failed: %s", cam.id, exc)
                return True
            obs[f"observation.images.{cam.name}"] = image
            images[cam.name] = image

        policy = session.policy
        if policy is None:
            return True
        t0 = time.monotonic()
        try:
            async with self._policy_exec_lock:
                action_tensor = await asyncio.to_thread(
                    _run_policy,
                    policy,
                    obs,
                    session.model.task_description,
                    session.preprocessor,
                    session.postprocessor,
                )
        except Exception as exc:
            logger.warning("policy.select_action failed: %s", exc)
            return True
        latency_ms = (time.monotonic() - t0) * 1000.0

        action_dict = _tensor_to_action_dict(action_tensor, policy)
        if session.model.max_action_magnitude is not None and action_dict:
            action_dict = _clamp_action(action_dict, session.model.max_action_magnitude)

        suppressed = False
        should_send = (
            not session.model.dry_run
            and action_dict
            and (not session.model.deadman_required or session.model.deadman_held)
        )
        if should_send:
            try:
                await self._robot_manager.send_action(session.model.robot_id, action_dict)
            except Exception as exc:
                logger.warning("send_action failed: %s", exc)
        elif not session.model.dry_run and session.model.deadman_required and not session.model.deadman_held:
            suppressed = True

        async with session.lock:
            update: dict[str, Any] = {"last_latency_ms": latency_ms}
            if suppressed:
                update["suppressed_steps"] = session.model.suppressed_steps + 1
            session.model = session.model.model_copy(update=update)
            subscriber_snapshot = list(session.subscribers)
        event = StepEvent(
            step=session.model.step,
            action=_action_list(action_tensor),
            latency_ms=latency_ms,
            image_hash=_hash_image_set(images),
            text=session.model.task_description or None,
        )
        for queue in subscriber_snapshot:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require(self, session_id: UUID) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise InferenceNotFoundError(f"inference session {session_id} not found")
        return session

    @staticmethod
    def _validate_feature_compatibility(policy: Any, robot_features: dict[str, dict]) -> None:
        """Fail fast if the policy expects keys the robot doesn't expose.

        Observation mismatches are **strict**: silently passing a policy
        that expects ``observation.state`` but receiving an empty dict
        from the robot lets garbage through the hot loop. Action
        mismatches are also strict — :meth:`RobotManagerProtocol.send_action`
        would otherwise raise ``KeyError`` mid-session.

        Skips validation when ``robot_features`` is empty (InMemory
        fallback / ``fake_devices`` mode) so E2E flows without hardware
        can still round-trip a stub policy.
        """
        robot_obs = dict(robot_features.get("observation") or {})
        robot_act = dict(robot_features.get("action") or {})
        if not robot_obs and not robot_act:
            return

        policy_obs = _policy_observation_features(policy)
        policy_act = _policy_action_features(policy)

        missing_obs = sorted(policy_obs - robot_obs.keys())
        if missing_obs:
            raise InferenceValidationError(
                "policy requires observation keys the robot doesn't expose: " + ", ".join(missing_obs)
            )
        missing_act = sorted(policy_act - robot_act.keys())
        if missing_act:
            raise InferenceValidationError(
                "policy requires action keys the robot doesn't expose: " + ", ".join(missing_act)
            )

    async def _open_camera_subscribers(self, session: _Session) -> None:
        for cam in session.cameras:
            await self._camera_manager.open(cam)
            session.camera_iters[cam.id] = self._camera_manager.subscribe(cam.id)

    async def _close_camera_subscribers(self, session: _Session) -> None:
        for cam_id, iterator in list(session.camera_iters.items()):
            with suppress(Exception):
                await iterator.aclose()  # type: ignore[attr-defined]
            session.camera_iters.pop(cam_id, None)

    async def _load_processors(self, repo_id: str, policy: Any) -> tuple[Any, Any]:
        """Load pre/post-processor pipelines for ``repo_id``.

        Uses ``make_pre_post_processors`` (loaded lazily to avoid importing
        the full lerobot policy stack at module import time). Falls back to
        ``(None, None)`` when the policy is a test stub (no ``.config``), the
        processor config is not found, or any other error occurs. Those
        sessions use the direct ``select_action`` path in ``_run_policy``.
        """
        config = getattr(policy, "config", None)
        if config is None:
            return None, None
        try:
            from lerobot.policies.factory import make_pre_post_processors

            preprocessor, postprocessor = await asyncio.to_thread(make_pre_post_processors, config, repo_id)
            logger.debug("processor pipeline loaded for %s", repo_id)
            return preprocessor, postprocessor
        except Exception as exc:  # noqa: BLE001
            logger.debug("processor pipeline not available for %s: %s", repo_id, exc)
            return None, None

    async def _emit(self, session: _Session, event: StepEvent) -> None:
        async with session.lock:
            queues = list(session.subscribers)
        for queue in queues:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)


# ---------------------------------------------------------------------------
# Module-level helpers (kept free of service state for thread-pool use)
# ---------------------------------------------------------------------------


def _run_policy(
    policy: Any,
    obs: dict[str, np.ndarray],
    task_description: str,
    preprocessor: Any = None,
    postprocessor: Any = None,
) -> Any:
    """Call ``policy.select_action`` safely. Runs on a worker thread.

    When *preprocessor* and *postprocessor* are provided (loaded via
    ``make_pre_post_processors``), delegates to :func:`predict_action` from
    ``lerobot.common.control_utils``.  That helper converts raw numpy arrays
    to PyTorch tensors, runs the processor pipeline (normalisation, language
    tokenisation, device placement), calls ``select_action``, and
    unnormalises the action.

    Without processors (test stubs, policies without a saved processor
    config), falls through to a bare ``select_action`` call.  Language-
    conditioned policies that expose a ``task`` kwarg are tried first; a
    :exc:`TypeError` indicates a classic policy (act, diffusion, …) that
    ignores the task argument.
    """
    if preprocessor is not None and postprocessor is not None:
        import torch

        from lerobot.common.control_utils import predict_action

        config = getattr(policy, "config", None)
        device_str = getattr(config, "device", None) or "cpu"
        device = torch.device(device_str)
        return predict_action(
            observation=obs,
            policy=policy,
            device=device,
            preprocessor=preprocessor,
            postprocessor=postprocessor,
            use_amp=False,
            task=task_description or None,
        )
    # Fallback: pass obs dict directly (works for stubs + legacy policies)
    if task_description:
        try:
            return policy.select_action(obs, task=task_description)
        except TypeError:
            pass
    return policy.select_action(obs)


def _tensor_to_action_dict(action: Any, policy: Any) -> dict[str, float]:
    """Unpack the policy's action tensor into a ``{name: float}`` dict.

    Falls back to a numeric index mapping (``joint_0``, ``joint_1``, ...)
    when the policy doesn't expose ``action_features`` — still gives
    ``send_action`` something to work with instead of silently dropping.
    """
    values = _action_list(action)
    names = _action_feature_names(policy)
    if names and len(names) == len(values):
        return dict(zip(names, values, strict=False))
    return {f"joint_{i}": v for i, v in enumerate(values)}


def _action_feature_names(policy: Any) -> list[str]:
    features = getattr(policy, "action_features", None)
    if isinstance(features, dict):
        return list(features.keys())
    return []


def _policy_observation_features(policy: Any) -> set[str]:
    """Collect the observation keys the policy expects.

    Prefers ``policy.config.input_features`` (populated by
    :class:`PreTrainedConfig`) and falls back to
    ``policy.observation_features`` / ``policy.config.observation_features``
    for older artefacts that haven't been round-tripped through the new
    config shape.
    """
    config = getattr(policy, "config", None)
    for source in (
        getattr(config, "input_features", None),
        getattr(config, "observation_features", None),
        getattr(policy, "observation_features", None),
    ):
        if isinstance(source, dict):
            return set(source.keys())
    return set()


def _policy_action_features(policy: Any) -> set[str]:
    features = getattr(policy, "action_features", None)
    if isinstance(features, dict):
        return set(features.keys())
    config = getattr(policy, "config", None)
    features = getattr(config, "output_features", None) or getattr(config, "action_features", None)
    if isinstance(features, dict):
        return set(features.keys())
    return set()


def _action_list(action: Any) -> list[float]:
    """Flatten any torch tensor / ndarray / list-of-numbers to ``list[float]``."""
    if action is None:
        return []
    try:
        # torch.Tensor has tolist()
        values = action.tolist()
    except AttributeError:
        try:
            values = list(action)
        except TypeError:
            return [float(action)]
    if isinstance(values, (int, float)):
        return [float(values)]
    flat: list[float] = []
    _flatten(values, flat)
    return flat


def _clamp_action(action: dict[str, float], limit: float) -> dict[str, float]:
    """Clamp each element of ``action`` to ``[-limit, +limit]``.

    Applied on top of the adapter-level safety clamp (``max_relative_target``
    etc.) so the operator can tighten the operating envelope per session
    without touching adapter config.
    """
    bound = float(limit)
    return {k: max(-bound, min(bound, float(v))) for k, v in action.items()}


def _flatten(obj: Any, out: list[float]) -> None:
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _flatten(item, out)
    else:
        out.append(float(obj))


def _hash_image_set(images: dict[str, np.ndarray]) -> str | None:
    """Short content-addressed hash of all frame bytes for this tick."""
    if not images:
        return None
    digest = hashlib.sha256()
    for name in sorted(images):
        digest.update(name.encode("utf-8"))
        digest.update(images[name].tobytes())
    return digest.hexdigest()[:16]


__all__ = [
    "InferenceConflictError",
    "InferenceError",
    "InferenceNotFoundError",
    "InferenceService",
    "InferenceSession",
    "InferenceStatus",
    "InferenceValidationError",
    "PolicyCache",
    "PolicyDescriptor",
    "PolicyLoader",
    "StartRequest",
    "StepEvent",
    "scan_hf_cache",
]
