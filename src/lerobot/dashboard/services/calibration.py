"""Calibration session controller.

Implements the backend half of the Task #15 calibration wizard. Contract
v3 (confirmed with frontend-architect-3 on 2026-04-14):

* REST is authoritative — ``start`` / ``ack`` / ``cancel`` / ``status`` /
  ``latest``. The ``status`` snapshot lets the UI resume after WS drops.
* WS ``/ws/robots/{robot_id}/calibrate`` is a read-only event stream
  (``step`` / ``done``) plus a 30s client heartbeat. All user actions go
  through REST.

Per-robot-type step sequences live on :func:`default_step_plan`. The
controller is deliberately event-driven — no persistent background task
per session. ``start`` publishes the first ``step``; ``ack`` advances
the cursor and publishes the next ``step`` (or ``done`` after the last).
``cancel`` publishes ``done`` with ``result: "cancelled"`` and tears the
session down. This keeps the TestClient-friendly behaviour where every
interaction is driven by an incoming HTTP call.

Joint-feedback streaming is intentionally deferred — it lands alongside
the real per-step motor-read logic in a follow-up PR. The wire format
already declares the ``joint_feedback`` event so the FE state machine
doesn't change once it arrives.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lerobot.dashboard.services.robot_manager import RobotManagerProtocol

logger = logging.getLogger(__name__)

# Sentinel put on a subscriber queue when the session terminates.
_TERMINAL = object()


# ---------------------------------------------------------------------------
# Error taxonomy (mapped to HTTP codes in the REST layer)
# ---------------------------------------------------------------------------


class CalibrationError(RuntimeError):
    """Base class for calibration controller errors."""


class RobotNotConnectedError(CalibrationError):
    """Attempted to start a session on an offline robot."""


class SessionAlreadyActiveError(CalibrationError):
    """A session already exists for this robot."""

    def __init__(self, existing_session_id: str) -> None:
        super().__init__(f"session {existing_session_id} is already active")
        self.existing_session_id = existing_session_id


class SessionNotFoundError(CalibrationError):
    """No active session for the given robot / session_id pair."""


class SessionMismatchError(CalibrationError):
    """session_id or step_id on an ``ack`` doesn't match the active session."""

    def __init__(self, detail: str, current_step_id: str | None) -> None:
        super().__init__(detail)
        self.current_step_id = current_step_id


# ---------------------------------------------------------------------------
# Step plan per robot_type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationStep:
    step_id: str
    prompt: str
    instruction: str


_SO_FOLLOWER_PLAN: tuple[CalibrationStep, ...] = (
    CalibrationStep(
        "home_pose",
        "Move the arm to the home pose",
        "Hold each joint at its mechanical mid-range. Press Next when the arm is steady.",
    ),
    CalibrationStep(
        "range_sweep",
        "Sweep each joint through its full range",
        "Rotate joints 1-5 slowly from one mechanical stop to the other, then return to home.",
    ),
    CalibrationStep(
        "gripper_close",
        "Close the gripper",
        "Fully close the gripper until the fingers touch; keep zero payload.",
    ),
    CalibrationStep(
        "gripper_open",
        "Open the gripper",
        "Fully open the gripper; let it rest at the mechanical stop.",
    ),
    CalibrationStep(
        "verify_angles",
        "Verify joint angles",
        "Return the arm to the home pose. Confirm the displayed angles look right, then finish.",
    ),
)

_KOCH_FOLLOWER_PLAN: tuple[CalibrationStep, ...] = (
    CalibrationStep(
        "home_pose",
        "Move the arm to the home pose",
        "Hold each joint at the mechanical mid-range and press Next.",
    ),
    CalibrationStep(
        "range_sweep",
        "Sweep joints through their range",
        "Rotate every joint from stop to stop and back.",
    ),
    CalibrationStep(
        "verify_angles",
        "Verify joint angles",
        "Return to home pose and confirm the readings match your posture.",
    ),
)

_UR_PLAN: tuple[CalibrationStep, ...] = (
    CalibrationStep(
        "verify_tcp_offset",
        "Verify the TCP offset",
        "Confirm the tool-center-point offset on the teach pendant matches the "
        "fixture in use. UR arms are factory-calibrated — this is a sanity check.",
    ),
    CalibrationStep(
        "verify_payload",
        "Verify the end-effector payload",
        "Confirm the payload mass and centre-of-gravity match the attached tool.",
    ),
)


_STEP_PLANS: dict[str, tuple[CalibrationStep, ...]] = {
    "so100_follower": _SO_FOLLOWER_PLAN,
    "so101_follower": _SO_FOLLOWER_PLAN,
    "koch_follower": _KOCH_FOLLOWER_PLAN,
    "ur": _UR_PLAN,
}


def default_step_plan(robot_type: str) -> tuple[CalibrationStep, ...]:
    """Return the declared step plan for ``robot_type``.

    Unknown types get a single ``manual`` step so the wizard UI still
    renders something actionable instead of failing — the real
    calibration routine for that robot is a follow-up task.
    """
    plan = _STEP_PLANS.get(robot_type)
    if plan is not None:
        return plan
    return (
        CalibrationStep(
            "manual",
            f"Calibrate {robot_type} manually",
            "No automated calibration plan is registered for this robot type. "
            "Complete the vendor-specific calibration routine, then press Next.",
        ),
    )


# ---------------------------------------------------------------------------
# Wire-format pydantic models
# ---------------------------------------------------------------------------


class CalibrationStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    robot_type: str
    total_steps: int = Field(ge=1)
    step_ids: list[str]


class CalibrationAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    step_id: str


class CalibrationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str


class CalibrationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    robot_type: str
    step_id: str
    step_index: int = Field(ge=0)
    total_steps: int = Field(ge=1)
    progress: float = Field(ge=0.0, le=1.0)
    awaiting_user_input: bool
    started_at: datetime


class CalibrationSummary(BaseModel):
    """Persisted summary of the last completed calibration."""

    model_config = ConfigDict(extra="forbid")

    robot_type: str
    calibration_id: str
    completed_at: datetime
    result: Literal["ok", "error", "cancelled"]
    step_ids: list[str]
    error: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Internal session state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Session:
    session_id: str
    robot_id: UUID
    robot_type: str
    plan: tuple[CalibrationStep, ...]
    step_index: int = 0
    progress: float = 0.0
    awaiting_user_input: bool = True
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    subscribers: list[asyncio.Queue[Any]] = field(default_factory=list)

    @property
    def current_step(self) -> CalibrationStep:
        return self.plan[self.step_index]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class CalibrationController:
    """Per-robot session registry + event bus.

    One session at a time per robot. All public methods are async and
    thread-safe across asyncio tasks via a single :class:`asyncio.Lock`.
    The state machine advances on REST events only — there is no
    persistent background task per session.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[UUID, _Session] = {}
        self._latest: dict[UUID, CalibrationSummary] = {}

    # ----- REST handlers --------------------------------------------------

    async def start(
        self,
        robot_id: UUID,
        robot_type: str,
        robot_manager: RobotManagerProtocol,
    ) -> CalibrationStartResponse:
        if not await robot_manager.is_connected(robot_id):
            raise RobotNotConnectedError(f"robot {robot_id} is not connected")

        plan = default_step_plan(robot_type)
        async with self._lock:
            existing = self._sessions.get(robot_id)
            if existing is not None:
                raise SessionAlreadyActiveError(existing.session_id)
            session = _Session(
                session_id=f"cal-{secrets.token_hex(8)}",
                robot_id=robot_id,
                robot_type=robot_type,
                plan=plan,
            )
            self._sessions[robot_id] = session
            self._publish_step_locked(session)

        return CalibrationStartResponse(
            session_id=session.session_id,
            robot_type=robot_type,
            total_steps=len(plan),
            step_ids=[s.step_id for s in plan],
        )

    async def ack(self, robot_id: UUID, session_id: str, step_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(robot_id)
            if session is None or session.session_id != session_id:
                raise SessionMismatchError(
                    "session_id does not match an active session", current_step_id=None
                )
            current = session.current_step.step_id
            if not session.awaiting_user_input:
                raise SessionMismatchError(
                    "server is not awaiting user input for this step",
                    current_step_id=current,
                )
            if step_id != current:
                raise SessionMismatchError(
                    f"step_id {step_id!r} does not match the active step {current!r}",
                    current_step_id=current,
                )

            session.progress = 1.0
            next_index = session.step_index + 1
            if next_index < len(session.plan):
                session.step_index = next_index
                session.progress = 0.0
                session.awaiting_user_input = True
                self._publish_step_locked(session)
                return
            # Last step acknowledged — finalise the session.
            self._finalise_locked(session, result="ok")

    async def cancel(self, robot_id: UUID, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(robot_id)
            if session is None or session.session_id != session_id:
                raise SessionNotFoundError(f"no active session {session_id!r}")
            self._finalise_locked(session, result="cancelled")

    async def get_status(self, robot_id: UUID) -> CalibrationStatusResponse | None:
        async with self._lock:
            session = self._sessions.get(robot_id)
            if session is None:
                return None
            step = session.current_step
            return CalibrationStatusResponse(
                session_id=session.session_id,
                robot_type=session.robot_type,
                step_id=step.step_id,
                step_index=session.step_index,
                total_steps=len(session.plan),
                progress=session.progress,
                awaiting_user_input=session.awaiting_user_input,
                started_at=session.started_at,
            )

    async def latest(self, robot_id: UUID) -> CalibrationSummary | None:
        async with self._lock:
            summary = self._latest.get(robot_id)
        return summary.model_copy() if summary is not None else None

    # ----- WS subscription ------------------------------------------------

    async def subscribe(self, robot_id: UUID, session_id: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
        async with self._lock:
            session = self._sessions.get(robot_id)
            if session is None or session.session_id != session_id:
                raise SessionNotFoundError(f"no active session {session_id!r}")
            # Replay the current step so late subscribers don't need a REST round-trip.
            await queue.put(self._step_event(session))
            session.subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is _TERMINAL:
                    return
                yield item
        finally:
            async with self._lock:
                session = self._sessions.get(robot_id)
                if session is not None:
                    try:
                        session.subscribers.remove(queue)
                    except ValueError:
                        pass

    # ----- internals (must hold self._lock) ------------------------------

    @staticmethod
    def _step_event(session: _Session) -> dict[str, Any]:
        step = session.current_step
        return {
            "type": "step",
            "step_id": step.step_id,
            "prompt": step.prompt,
            "instruction": step.instruction,
            "progress": session.progress,
            "awaiting_user_input": session.awaiting_user_input,
            "image_ref": None,
        }

    def _publish_step_locked(self, session: _Session) -> None:
        event = self._step_event(session)
        for queue in list(session.subscribers):
            _enqueue_drop_oldest(queue, event)

    def _finalise_locked(
        self,
        session: _Session,
        *,
        result: Literal["ok", "error", "cancelled"],
        error: dict[str, str] | None = None,
    ) -> None:
        summary = CalibrationSummary(
            robot_type=session.robot_type,
            calibration_id=session.session_id,
            completed_at=datetime.now(UTC),
            result=result,
            step_ids=[s.step_id for s in session.plan],
            error=error,
        )
        done_event: dict[str, Any] = {
            "type": "done",
            "result": result,
            "calibration_id": session.session_id,
        }
        if error is not None:
            done_event["error"] = error
        for queue in list(session.subscribers):
            _enqueue_drop_oldest(queue, done_event)
            _enqueue_drop_oldest(queue, _TERMINAL)
        session.subscribers.clear()
        self._sessions.pop(session.robot_id, None)
        self._latest[session.robot_id] = summary


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
    "CalibrationAckRequest",
    "CalibrationCancelRequest",
    "CalibrationController",
    "CalibrationError",
    "CalibrationStartResponse",
    "CalibrationStatusResponse",
    "CalibrationStep",
    "CalibrationSummary",
    "RobotNotConnectedError",
    "SessionAlreadyActiveError",
    "SessionMismatchError",
    "SessionNotFoundError",
    "default_step_plan",
]
