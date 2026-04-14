"""WebSocket handler for ``/ws/robots/{robot_id}/teleop`` (task #11/#12).

One WebSocket connection per operator. Only one operator may hold a
given robot at a time; a second connect is immediately closed with code
4001 (conflict).

Protocol (client → server):
  heartbeat : {seq, ts_client_ms, type:"heartbeat", payload:{}}
  deadman   : {seq, ts_client_ms, type:"deadman",   payload:{held:bool}}
  action    : {seq, ts_client_ms, type:"action",    payload:{values:{joint:float}}}
  input     : {seq, ts_client_ms, type:"input",     payload:{kind:"keyboard"|"mouse"|"gamepad",...}}
  mode      : {seq, ts_client_ms, type:"mode",      payload:{mode:"idle"|"teleop"|"replay"}}

Protocol (server → client):
  state     : {seq, ts_server_ms, type:"state",     payload:{state:str, reason?:str}}
  ack       : {seq, ts_server_ms, type:"ack",       payload:{seq:int, received_at_ms:float}}
  error     : {seq, ts_server_ms, type:"error",     payload:{code:str, message:str}}
  telemetry : {seq, ts_server_ms, type:"telemetry", payload:{deadman, forwarded, ...}}

Close codes:
  1000  normal
  4001  conflict — another operator already connected
  4002  robot_offline — robot not connected in robot_manager
  1011  server error

Input source wiring (task #12):
  GamepadEventSource — created for every session; ``kind:"gamepad"`` WS
    frames are routed to ``source.feed()`` which translates axes via
    ``GamepadMapping`` and enqueues action vectors for the dispatcher.
    By default the mapping is empty (no joints assigned); operator
    config will populate it once ``TeleopEntry`` carries axis→joint tables.

  LeaderArmEventSource — attached when the session factory provides one
    via the ``leader_arm_factory`` parameter on
    ``build_teleop_ws_router()``. A connect failure is logged and pushed
    to the client as an ``error`` frame; the session continues so the
    operator can still use gamepad / direct action frames.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from lerobot.dashboard.core.state import AppState
from lerobot.dashboard.teleop.adapters.gamepad import GamepadEventSource, LinearAxisMapping
from lerobot.dashboard.teleop.adapters.leader_arm import LeaderArmEventSource, TeleoperatorPoller
from lerobot.dashboard.teleop.deadman import HEARTBEAT_TIMEOUT_MS, DeadmanStateMachine
from lerobot.dashboard.teleop.dispatcher import TeleopDispatcher
from lerobot.dashboard.teleop.protocol import (
    ClientFrame,
    ClientFrameType,
    GamepadEvent,
    ProtocolError,
    ServerFrame,
    ServerFrameType,
    parse_teleop_event,
)
from lerobot.dashboard.teleop.validator import ActionSpec, ActionValidator

logger = logging.getLogger(__name__)

# Robots currently owned by a teleop session (robot_id → task).
# Keyed as strings to avoid UUID hashing inconsistencies.
_ACTIVE: dict[str, asyncio.Task[None]] = {}
_ACTIVE_LOCK = asyncio.Lock()

# Default permissive ActionSpec — allows any float in [-1,1] for unknown joints.
# Replaced by robot's declared spec once TeleopEntry carries one.
_DEFAULT_SPEC = ActionSpec(
    joint_names=("__fallback__",),
    low=(-1.0,),
    high=(1.0,),
    rate_limit_hz=200.0,
)

_TELEMETRY_HZ = 1.0

# Type alias for the optional leader-arm factory injected by tests or the
# app factory. Receives the robot UUID and returns a ready-to-start source
# (or None if no leader arm is configured for this robot).
LeaderArmFactory = Callable[[UUID], LeaderArmEventSource | None]


def build_teleop_ws_router(
    *,
    leader_arm_factory: LeaderArmFactory | None = None,
) -> APIRouter:
    """Build and return the teleop WebSocket sub-router.

    Parameters
    ----------
    leader_arm_factory:
        Optional factory called with ``robot_id`` at session start. If it
        returns a :class:`LeaderArmEventSource`, the session attaches it to
        the dispatcher. Primarily used for testing with a
        :class:`StubTeleoperator`; production code would wire the real
        hardware factory here once teleoperator config is fully integrated.
    """
    router = APIRouter()

    @router.websocket("/robots/{robot_id}/teleop")
    async def teleop_ws(websocket: WebSocket, robot_id: str) -> None:
        state: AppState = websocket.app.state.dashboard

        # ── Validate robot id ───────────────────────────────────────────────
        try:
            rid = UUID(robot_id)
        except ValueError:
            await websocket.accept()
            await _safe_close(websocket, 4002, "invalid robot_id")
            return

        await websocket.accept()

        robot_manager = state.robot_manager
        teleop_manager = state.teleop_manager

        # ── robot_offline guard ─────────────────────────────────────────────
        try:
            is_up = await robot_manager.is_connected(rid)
        except Exception:
            logger.exception("robot_manager.is_connected raised for %s", robot_id)
            await _safe_close(websocket, 4002, "robot_offline")
            return

        if not is_up:
            logger.info("teleop: robot %s offline", robot_id)
            await _safe_close(websocket, 4002, "robot_offline")
            return

        # ── conflict guard ──────────────────────────────────────────────────
        async with _ACTIVE_LOCK:
            if robot_id in _ACTIVE and not _ACTIVE[robot_id].done():
                logger.warning("teleop: robot %s already owned; rejecting", robot_id)
                await _safe_close(websocket, 4001, "conflict")
                return

        # ── session setup ───────────────────────────────────────────────────
        deadman = DeadmanStateMachine()
        validator = ActionValidator(_DEFAULT_SPEC)
        seq_out = _SeqCounter()

        dispatcher = TeleopDispatcher(
            robot_id=rid,
            robot_manager=robot_manager,
            validator=validator,
            deadman=deadman,
            teleop_manager=teleop_manager,
            # teleop_id — use the robot_id as a proxy until TeleopEntry is
            # resolved. The InMemoryTeleopManager ignores this id anyway.
            teleop_id=rid,
        )

        # GamepadEventSource: always created so gamepad WS frames are
        # immediately available. The default mapping has no joint assignments
        # (no actions produced) until the operator configures axis→joint
        # mapping via TeleopEntry config.
        gamepad_source = GamepadEventSource(mapping=LinearAxisMapping(axis_joints=()))

        async def _run_session() -> None:
            _closed = False

            async def safe_send(data: dict[str, Any]) -> None:
                if _closed:
                    return
                await _safe_send_json(websocket, data)

            async def push_state(state_str: str, reason: str | None = None) -> None:
                payload: dict[str, Any] = {"state": state_str}
                if reason:
                    payload["reason"] = reason
                await safe_send(
                    ServerFrame(
                        seq=seq_out.next(),
                        ts_server_ms=_now_ms(),
                        type=ServerFrameType.STATE,
                        payload=payload,
                    ).to_json()
                )

            async def push_ack(client_seq: int) -> None:
                await safe_send(
                    ServerFrame(
                        seq=seq_out.next(),
                        ts_server_ms=_now_ms(),
                        type=ServerFrameType.ACK,
                        payload={"seq": client_seq, "received_at_ms": _now_ms()},
                    ).to_json()
                )

            async def push_error(code: str, message: str) -> None:
                await safe_send(
                    ServerFrame(
                        seq=seq_out.next(),
                        ts_server_ms=_now_ms(),
                        type=ServerFrameType.ERROR,
                        payload={"code": code, "message": message},
                    ).to_json()
                )

            async def telemetry_loop() -> None:
                interval = 1.0 / _TELEMETRY_HZ
                while True:
                    await asyncio.sleep(interval)
                    payload = {
                        "deadman": deadman.state.value,
                        "lockout_reason": None,
                        "forwarded": dispatcher.forwarded,
                        "dropped_deadman": dispatcher.dropped_deadman,
                        "dropped_validation": dispatcher.dropped_validation,
                        "aux_events_forwarded": dispatcher.aux_events_forwarded,
                        "aux_events_dropped": dispatcher.aux_events_dropped,
                        "last_action_ts_ms": None,
                    }
                    await safe_send(
                        ServerFrame(
                            seq=seq_out.next(),
                            ts_server_ms=_now_ms(),
                            type=ServerFrameType.TELEMETRY,
                            payload=payload,
                        ).to_json()
                    )

            telemetry_task = asyncio.create_task(telemetry_loop(), name="teleop_telemetry")

            try:
                # Attach gamepad source — passive (just needs start() called so
                # it accepts feed() calls from the WS frame handler).
                await dispatcher.attach(gamepad_source)

                # Optionally attach leader arm source.
                if leader_arm_factory is not None:
                    leader_src = leader_arm_factory(rid)
                    if leader_src is not None:
                        try:
                            await dispatcher.attach(leader_src)
                            logger.info("teleop: leader arm attached for robot %s", robot_id)
                        except Exception as exc:
                            logger.warning(
                                "teleop: leader arm connect failed for robot %s: %s",
                                robot_id,
                                exc,
                            )
                            await push_error(
                                "leader_arm_connect_failed",
                                f"Leader arm failed to connect: {exc}",
                            )
                            # Session continues — operator can still use gamepad /
                            # direct action frames.

                # Push initial IDLE state.
                await push_state("idle")

                while True:
                    raw = await websocket.receive_json()
                    try:
                        frame = ClientFrame.from_json(raw)
                    except ProtocolError as exc:
                        await push_error("protocol_error", str(exc))
                        continue

                    now_ms = _now_ms()
                    prev_deadman = deadman.state

                    match frame.type:
                        case ClientFrameType.HEARTBEAT:
                            deadman.heartbeat(now_ms)

                        case ClientFrameType.DEADMAN:
                            held = bool(frame.payload.get("held", False))
                            if held:
                                deadman.press(now_ms)
                            else:
                                deadman.release(now_ms)

                        case ClientFrameType.ACTION:
                            values = frame.payload.get("values") or {}
                            await dispatcher._process(values)
                            await push_ack(frame.seq)

                        case ClientFrameType.INPUT:
                            try:
                                event = parse_teleop_event(frame.payload)
                            except ProtocolError as exc:
                                await push_error("bad_input", str(exc))
                                continue
                            # Gamepad events feed the action source directly so
                            # axis values flow through the deadman gate and
                            # validator on the same code path as direct action
                            # frames (not the aux handle_event path).
                            if isinstance(event, GamepadEvent):
                                await gamepad_source.feed(event)
                            # Always forward to aux channel for raw event logging,
                            # macro triggering, or any future teleop_manager use.
                            await dispatcher.handle_event(event)
                            await push_ack(frame.seq)

                        case ClientFrameType.MODE:
                            # Mode is stored but not enforced in MVP.
                            await push_ack(frame.seq)

                        case _:
                            pass  # unknown types silently ignored

                    # Push state frame on any deadman transition.
                    new_deadman = deadman.tick(now_ms)
                    if new_deadman != prev_deadman:
                        await push_state(new_deadman.value)

            except WebSocketDisconnect:
                logger.info("teleop: robot %s WS disconnected", robot_id)
            except Exception:
                logger.exception("teleop: unexpected error for robot %s", robot_id)
                await _safe_close(websocket, 1011, "server_error")
            finally:
                _closed = True
                telemetry_task.cancel()
                await asyncio.gather(telemetry_task, return_exceptions=True)
                await dispatcher.close()
                async with _ACTIVE_LOCK:
                    _ACTIVE.pop(robot_id, None)
                logger.info("teleop: robot %s session cleaned up", robot_id)

        task = asyncio.create_task(_run_session(), name=f"teleop_{robot_id}")
        async with _ACTIVE_LOCK:
            _ACTIVE[robot_id] = task

        try:
            await task
        except asyncio.CancelledError:
            pass

    return router


# ── helpers ────────────────────────────────────────────────────────────────


class _SeqCounter:
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def _now_ms() -> float:
    return time.monotonic() * 1000.0


async def _safe_send_json(ws: WebSocket, data: dict[str, Any]) -> None:
    try:
        await ws.send_json(data)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _safe_close(ws: WebSocket, code: int, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason)
    except (WebSocketDisconnect, RuntimeError):
        pass
