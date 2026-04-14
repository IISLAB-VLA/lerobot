"""Dataset recording service for the dashboard (task #13).

Spins up a per-session async capture loop that pulls frames from
:class:`CameraManagerProtocol` and observations from
:class:`RobotManagerProtocol`, writes them through the existing
:class:`~lerobot.datasets.lerobot_dataset.LeRobotDataset` pipeline, and
exposes 1 Hz progress events to subscribers (HTTP surface + WS channel).

Design notes
------------
* **One session per robot**. The service refuses ``start`` for a robot
  that already has an active session so two capture loops can't race on
  the same hardware or dataset directory.
* **Feature schema at session start**. The recorder asks the robot
  manager for its hardware-level ``observation`` and ``action`` feature
  dicts (see :meth:`RobotManagerProtocol.get_features`) and normalises
  them into the LeRobotDataset feature spec via
  :func:`lerobot.utils.feature_utils.hw_to_dataset_features`.
* **Fake-devices friendly**. When the adapter has no real hardware
  bound (InMemory fallback or ``LEROBOT_DASHBOARD_FAKE_DEVICES=1``), the
  robot returns empty feature dicts and the recorder still produces a
  valid, just-images-no-joints dataset so E2E tests can round-trip
  record → stop → on-disk inspection without a robot.
* **Capture loop isolation**. Each session owns an ``asyncio.Task``; it
  never takes the service lock inside the loop so stop/list/subscribe
  calls from HTTP handlers don't block the capture cadence. Per-session
  state is protected by the session's own lock.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.services.camera_manager import CameraManagerProtocol
from lerobot.dashboard.services.registry import Registry
from lerobot.dashboard.services.registry_models import CameraEntry, RobotEntry
from lerobot.dashboard.services.robot_manager import RobotManagerProtocol

logger = logging.getLogger(__name__)


class RecorderError(Exception):
    """Base class for recorder errors surfaced to the API layer."""


class RecorderNotFoundError(RecorderError):
    """Session id does not exist."""


class RecorderConflictError(RecorderError):
    """Start refused because the robot already has an active session, or
    stop refused because the session is no longer running."""


class RecorderValidationError(RecorderError):
    """Inputs failed validation (dataset name shape, robot offline, etc.)."""


RecordingStatus = Literal["starting", "recording", "stopping", "saved", "discarded", "failed"]

_TERMINAL_STATUSES: frozenset[str] = frozenset({"saved", "discarded", "failed"})
_DATASET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class StartRequest(BaseModel):
    """Payload accepted by :meth:`RecorderService.start`."""

    model_config = ConfigDict(extra="forbid")

    robot_id: UUID
    dataset_name: str = Field(..., min_length=1, max_length=64)
    task_description: str = Field(..., min_length=1, max_length=500)
    fps: int = Field(..., ge=1, le=240)
    use_videos: bool = True


class RecordingSession(BaseModel):
    """Lightweight view of a recording session for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    robot_id: UUID
    dataset_name: str
    dataset_path: str
    task_description: str
    fps: int
    status: RecordingStatus
    frames_captured: int = 0
    drop_count: int = 0
    disk_bytes: int = 0
    started_at: datetime
    stopped_at: datetime | None = None
    error: str | None = None
    episode_index: int | None = None
    saved: bool = False


class ProgressEvent(BaseModel):
    """1 Hz heartbeat emitted to WS subscribers."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["progress", "stopped", "error"] = "progress"
    frames_captured: int = 0
    duration_s: float = 0.0
    disk_bytes: int = 0
    drop_count: int = 0
    saved: bool | None = None
    episode_index: int | None = None
    message: str | None = None


@dataclass(slots=True)
class _Session:
    """Internal mutable session state."""

    model: RecordingSession
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set[asyncio.Queue[ProgressEvent]] = field(default_factory=set)
    dataset_handle: Any = None  # LeRobotDataset (avoid import-at-module-load)
    robot_entry: RobotEntry | None = None
    cameras: list[CameraEntry] = field(default_factory=list)
    # Per-camera frame subscriber, populated on session start. ``None`` until
    # ``start()`` opens the cameras and wires up the iterators.
    camera_iters: dict[UUID, AsyncIterator[Any]] = field(default_factory=dict)
    # Graceful-stop signal. ``stop()`` sets this so the capture loop can
    # finish its current ``add_frame`` on the worker thread before exiting;
    # avoids cancelling in the middle of a parquet append and leaving
    # columns at mismatched lengths.
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    # Guards the actual ``dataset.add_frame`` write so ``stop``'s call to
    # ``save_episode`` / ``finalize`` can't race with a writer thread that
    # survived cooperative shutdown.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RecorderService:
    """Orchestrates :class:`_Session` lifecycles + the capture loop."""

    def __init__(
        self,
        registry: Registry,
        robot_manager: RobotManagerProtocol,
        camera_manager: CameraManagerProtocol,
        datasets_dir: Path,
    ) -> None:
        self._registry = registry
        self._robot_manager = robot_manager
        self._camera_manager = camera_manager
        self._datasets_dir = datasets_dir
        self._lock = asyncio.Lock()
        self._sessions: dict[UUID, _Session] = {}

    # ------------------------------------------------------------------
    # Public API — start / stop / introspection
    # ------------------------------------------------------------------

    async def start(self, req: StartRequest) -> RecordingSession:
        self._validate_dataset_name(req.dataset_name)
        robot = await self._registry.get_robot(req.robot_id)
        all_cameras = await self._registry.list_cameras()
        cam_map = {c.id: c for c in all_cameras}
        cameras = [cam_map[cid] for cid in robot.cameras if cid in cam_map]

        if not await self._robot_manager.is_connected(req.robot_id):
            raise RecorderValidationError(f"robot {req.robot_id} is not connected; /connect before recording")

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
                raise RecorderConflictError(
                    f"robot {req.robot_id} already has an active recording (session {active.model.id})"
                )

            dataset_path = self._datasets_dir / req.dataset_name
            model = RecordingSession(
                id=uuid4(),
                robot_id=req.robot_id,
                dataset_name=req.dataset_name,
                dataset_path=str(dataset_path),
                task_description=req.task_description,
                fps=req.fps,
                status="starting",
                started_at=datetime.now(UTC),
            )
            session = _Session(model=model, robot_entry=robot, cameras=cameras)
            self._sessions[model.id] = session

        try:
            features = await self._resolve_features(req, cameras)
            session.dataset_handle = await asyncio.to_thread(
                self._create_dataset, req, features, dataset_path, robot.robot_type
            )
            await self._open_camera_subscribers(session)
        except Exception as exc:
            await self._close_camera_subscribers(session)
            async with session.lock:
                session.model = session.model.model_copy(update={"status": "failed", "error": str(exc)})
            logger.exception("recorder: dataset creation failed for %s", req.dataset_name)
            raise RecorderError(f"dataset init failed: {exc}") from exc

        session.task = asyncio.create_task(self._run(session), name=f"recorder:{model.id}")
        async with session.lock:
            session.model = session.model.model_copy(update={"status": "recording"})
        return session.model.model_copy()

    async def _open_camera_subscribers(self, session: _Session) -> None:
        """Open each referenced camera and stash a subscribe() iterator on the
        session. Cameras already open (e.g. shared with the streaming stack)
        get a no-op ``open`` thanks to :class:`CameraManagerProtocol`'s
        idempotent contract; each subscriber is independent."""
        for cam in session.cameras:
            await self._camera_manager.open(cam)
            # ``subscribe`` is an async generator — calling returns the
            # iterator without awaiting. Store it so stop() can aclose() later.
            session.camera_iters[cam.id] = self._camera_manager.subscribe(cam.id)

    async def _close_camera_subscribers(self, session: _Session) -> None:
        """Release subscribe iterators. Idempotent so ``stop`` + ``close``
        can both call safely."""
        for cam_id, iterator in list(session.camera_iters.items()):
            with suppress(Exception):
                await iterator.aclose()  # type: ignore[attr-defined]
            session.camera_iters.pop(cam_id, None)

    async def stop(self, session_id: UUID, save: bool) -> RecordingSession:
        session = self._require(session_id)
        async with session.lock:
            if session.model.status in _TERMINAL_STATUSES:
                raise RecorderConflictError(f"session {session_id} is already {session.model.status}")
            if session.model.status == "stopping":
                raise RecorderConflictError(f"session {session_id} is already stopping")
            session.model = session.model.model_copy(update={"status": "stopping"})

        # Request cooperative shutdown and give the loop a short window to
        # finish any in-flight ``add_frame``. If the loop is stuck we fall
        # back to cancellation; the write_lock below still guards the
        # writer thread so finalize observes a consistent column state.
        session.stop_requested.set()
        if session.task is not None:
            try:
                await asyncio.wait_for(session.task, timeout=5.0)
            except TimeoutError:
                session.task.cancel()
                with suppress(asyncio.CancelledError):
                    await session.task
            except asyncio.CancelledError:
                pass

        await self._close_camera_subscribers(session)
        # Wait for any writer thread that survived cancellation before
        # flushing; running finalize concurrently with an in-flight
        # add_frame leaves columns at mismatched lengths in parquet.
        async with session.write_lock:
            summary = await asyncio.to_thread(self._finalize_dataset, session, save)
        async with session.lock:
            session.model = summary
        await self._emit(
            session,
            ProgressEvent(
                type="stopped",
                frames_captured=summary.frames_captured,
                duration_s=self._duration_s(summary),
                disk_bytes=summary.disk_bytes,
                drop_count=summary.drop_count,
                saved=summary.saved,
                episode_index=summary.episode_index,
            ),
        )
        return summary.model_copy()

    async def list(self) -> list[RecordingSession]:
        async with self._lock:
            return [s.model.model_copy() for s in self._sessions.values()]

    async def get(self, session_id: UUID) -> RecordingSession:
        return self._require(session_id).model.model_copy()

    async def subscribe(self, session_id: UUID) -> AsyncIterator[ProgressEvent]:
        """Yield progress events until the session terminates.

        Terminal sessions yield one final ``stopped`` / ``error`` event and
        then close; subscribers never block after that.
        """
        session = self._require(session_id)
        queue: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=64)
        async with session.lock:
            session.subscribers.add(queue)
            terminal = session.model.status in _TERMINAL_STATUSES
        try:
            if terminal:
                # Late subscriber — synthesize one terminal event and return.
                yield ProgressEvent(
                    type="stopped" if session.model.status != "failed" else "error",
                    frames_captured=session.model.frames_captured,
                    duration_s=self._duration_s(session.model),
                    disk_bytes=session.model.disk_bytes,
                    drop_count=session.model.drop_count,
                    saved=session.model.saved,
                    episode_index=session.model.episode_index,
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
        """Cancel every active session. Called from ``lifespan`` shutdown."""
        async with self._lock:
            active = [s for s in self._sessions.values() if s.model.status not in _TERMINAL_STATUSES]
        for session in active:
            with suppress(Exception):
                await self.stop(session.model.id, save=False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self, session_id: UUID) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise RecorderNotFoundError(f"recording session {session_id} not found")
        return session

    @staticmethod
    def _validate_dataset_name(name: str) -> None:
        if not _DATASET_NAME_RE.match(name):
            raise RecorderValidationError("dataset_name must match [a-z0-9][a-z0-9_-]{0,63}")

    async def _resolve_features(self, req: StartRequest, cameras: list[CameraEntry]) -> dict[str, dict]:
        """Build the LeRobotDataset feature dict for this session."""
        from lerobot.utils.constants import ACTION, OBS_STR
        from lerobot.utils.feature_utils import hw_to_dataset_features

        hw = await self._robot_manager.get_features(req.robot_id)
        obs_hw: dict[str, Any] = dict(hw.get("observation", {}))
        act_hw: dict[str, Any] = dict(hw.get("action", {}))

        # Overlay camera shapes (one entry per camera, keyed by camera.name).
        for cam in cameras:
            obs_hw[cam.name] = (cam.height, cam.width, 3)

        features: dict[str, dict] = {}
        features.update(hw_to_dataset_features(obs_hw, OBS_STR, req.use_videos))
        features.update(hw_to_dataset_features(act_hw, ACTION, req.use_videos))
        return features

    def _create_dataset(
        self,
        req: StartRequest,
        features: dict[str, dict],
        dataset_path: Path,
        robot_type: str,
    ) -> Any:
        """Create the LeRobotDataset handle. Runs on a worker thread."""
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        return LeRobotDataset.create(
            repo_id=f"dashboard/{req.dataset_name}",
            fps=req.fps,
            features=features,
            root=dataset_path,
            robot_type=robot_type,
            use_videos=req.use_videos,
        )

    def _finalize_dataset(self, session: _Session, save: bool) -> RecordingSession:
        model = session.model
        ds = session.dataset_handle
        stopped_at = datetime.now(UTC)
        try:
            if ds is None:
                return model.model_copy(update={"status": "discarded", "stopped_at": stopped_at})
            if save and model.frames_captured > 0:
                episode_index = getattr(getattr(ds, "meta", None), "total_episodes", 0)
                ds.save_episode()
                ds.finalize()
                return model.model_copy(
                    update={
                        "status": "saved",
                        "saved": True,
                        "stopped_at": stopped_at,
                        "episode_index": episode_index,
                        "disk_bytes": _dir_size(Path(model.dataset_path)),
                    }
                )
            # Discard path — drop buffered frames and remove the empty dataset dir.
            with suppress(Exception):
                ds.clear_episode_buffer(delete_images=True)
            with suppress(Exception):
                ds.finalize()
            return model.model_copy(update={"status": "discarded", "stopped_at": stopped_at})
        except Exception as exc:
            logger.exception("recorder: finalize failed for %s", model.dataset_name)
            return model.model_copy(
                update={
                    "status": "failed",
                    "error": str(exc),
                    "stopped_at": stopped_at,
                }
            )

    async def _run(self, session: _Session) -> None:
        """Capture loop — one frame per tick at the session's fps."""
        period = 1.0 / session.model.fps
        heartbeat_interval = 1.0
        next_heartbeat = time.monotonic() + heartbeat_interval
        try:
            while not session.stop_requested.is_set():
                tick = time.monotonic()
                drop = await self._capture_one_frame(session)
                async with session.lock:
                    new_frames = session.model.frames_captured + (0 if drop else 1)
                    new_drops = session.model.drop_count + (1 if drop else 0)
                    session.model = session.model.model_copy(
                        update={
                            "frames_captured": new_frames,
                            "drop_count": new_drops,
                        }
                    )
                now = time.monotonic()
                if now >= next_heartbeat:
                    await self._emit(
                        session,
                        ProgressEvent(
                            type="progress",
                            frames_captured=session.model.frames_captured,
                            duration_s=self._duration_s(session.model),
                            disk_bytes=_dir_size(Path(session.model.dataset_path)),
                            drop_count=session.model.drop_count,
                        ),
                    )
                    next_heartbeat = now + heartbeat_interval
                elapsed = time.monotonic() - tick
                # Wait for the next tick, but return immediately if ``stop``
                # signals — avoids paying a full fps period before exiting.
                try:
                    await asyncio.wait_for(
                        session.stop_requested.wait(),
                        timeout=max(0.0, period - elapsed),
                    )
                    return
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with session.lock:
                session.model = session.model.model_copy(update={"status": "failed", "error": str(exc)})
            await self._emit(
                session,
                ProgressEvent(type="error", message=str(exc)),
            )
            logger.exception("recorder: capture loop crashed for %s", session.model.id)

    async def _capture_one_frame(self, session: _Session) -> bool:
        """Compose one dataset frame (observation + camera frames) and push
        it into the LeRobotDataset writer. Returns ``True`` if the frame is
        dropped (robot read failed, camera stuck, or writer refused).

        Architecture note: ``robot_manager.read_observation`` is expected to
        return motor/joint state only — camera frames arrive via the
        ``camera_manager.subscribe`` iterators opened at session start.
        This separation matches the V4L2 / RealSense EBUSY constraints
        where only one consumer can hold the device handle; the camera
        manager is that consumer and multiplexes frames through its
        per-subscriber queue.
        """
        try:
            obs = await self._robot_manager.read_observation(session.model.robot_id)
        except Exception as exc:
            logger.warning("read_observation failed: %s", exc)
            return True

        frame: dict[str, Any] = {"task": session.model.task_description}
        for key, value in obs.items():
            # Forward observation.* keys verbatim so adapters can still
            # round-trip pre-prefixed state dicts.
            if key.startswith("observation."):
                frame[key] = value

        # Pull the latest frame from each camera subscriber. A subscriber
        # that stalls within a generous window is treated as a drop rather
        # than a fatal error — capture continues so an occasional hiccup
        # doesn't derail a long session. The timeout is a multiple of the
        # dataset period so normal scheduler jitter doesn't register as a
        # stall even when the recorder and camera fps match.
        period = 1.0 / session.model.fps
        stall_timeout = max(period * 3, 0.25)
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
            frame[f"observation.images.{cam.name}"] = image

        if session.dataset_handle is None:
            return True
        # Hold ``write_lock`` for the duration of the writer thread so
        # ``stop``'s finalize call waits behind any frame still appending
        # columns — prevents the "expected length N but got N-1" parquet
        # crash when the loop is cancelled mid-append.
        try:
            async with session.write_lock:
                await asyncio.to_thread(session.dataset_handle.add_frame, frame)
        except Exception as exc:
            logger.warning("add_frame failed: %s", exc)
            return True
        return False

    @staticmethod
    def _duration_s(model: RecordingSession) -> float:
        end = model.stopped_at or datetime.now(UTC)
        return max(0.0, (end - model.started_at).total_seconds())

    async def _emit(self, session: _Session, event: ProgressEvent) -> None:
        async with session.lock:
            queues = list(session.subscribers)
        for queue in queues:
            # Drop oldest event if a subscriber is slow — we'd rather lose a
            # heartbeat than block the capture loop. Terminal events always
            # go through; subscribers are expected to drain promptly.
            if event.type != "progress":
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
                continue
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            with suppress(OSError):
                total += entry.stat().st_size
    return total
