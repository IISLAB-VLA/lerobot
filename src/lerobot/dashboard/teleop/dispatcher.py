"""Multiplex teleop input sources into :meth:`RobotManager.send_action`.

The dispatcher is the piece the WS handler instantiates once a teleop
session is negotiated. It owns:

* a :class:`DeadmanStateMachine` — the safety gate; actions only forward
  while the machine reports ``ENGAGED``.
* an :class:`ActionValidator` — shape/range/rate validator; sanitises
  each payload before it touches the robot.
* a collection of :class:`TeleopEventSource` instances — gamepad frames
  parsed from the socket, leader-arm poll loops, etc.

Each attached source runs on its own consumer task. All consumers call
into the same :meth:`_process` path so deadman gating and validator
state are shared — two sources cannot each carry their own rate-limit
clock and double the effective command rate.

Lifecycle:

1. ``await dispatcher.attach(source)`` — start the source and spawn its
   consumer task.
2. Source yields ``dict[str, float]`` payloads; dispatcher validates +
   forwards to ``robot_manager.send_action(robot_id, action)``.
3. ``await dispatcher.close()`` — cancel every consumer task, stop every
   source. Idempotent so the WS handler can fire it from a ``finally``.

The ``handle_input`` aux channel (raw keyboard/mouse/gamepad events via
:class:`TeleopEvent`) is intentionally not wired here yet — it lands as
a second commit once :class:`TeleopManagerProtocol` has a concrete
implementation to forward to.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable
from uuid import UUID

from lerobot.dashboard.services.robot_manager import RobotManagerProtocol
from lerobot.dashboard.teleop.adapters.base import SourceState, TeleopEventSource
from lerobot.dashboard.teleop.deadman import DeadmanStateMachine
from lerobot.dashboard.teleop.validator import ActionValidationError, ActionValidator

logger = logging.getLogger(__name__)


class TeleopDispatcher:
    """Wire a robot to one or more live input sources."""

    def __init__(
        self,
        robot_id: UUID,
        robot_manager: RobotManagerProtocol,
        validator: ActionValidator,
        deadman: DeadmanStateMachine,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._robot_id = robot_id
        self._robot_manager = robot_manager
        self._validator = validator
        self._deadman = deadman
        # Expose ms-resolution time to the validator while keeping the
        # injection point tiny for tests.
        self._clock = clock
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._sources: dict[int, TeleopEventSource] = {}
        self._closed = False
        # Stats for the telemetry channel — cheap counters so the WS
        # handler can surface "how many dropped for safety / validation".
        self.forwarded: int = 0
        self.dropped_deadman: int = 0
        self.dropped_validation: int = 0

    async def attach(self, source: TeleopEventSource) -> None:
        if self._closed:
            raise RuntimeError("dispatcher is closed")
        key = id(source)
        if key in self._tasks:
            return
        await source.start()
        self._sources[key] = source
        self._tasks[key] = asyncio.create_task(
            self._consume(source), name=f"TeleopDispatcher.consume[{key:x}]"
        )

    async def detach(self, source: TeleopEventSource) -> None:
        key = id(source)
        task = self._tasks.pop(key, None)
        self._sources.pop(key, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await source.stop()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("error stopping source during detach")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Snapshot then drain so detach-while-iterating is safe.
        for source in list(self._sources.values()):
            await self.detach(source)

    async def _consume(self, source: TeleopEventSource) -> None:
        try:
            async for payload in source.events():
                await self._process(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("source %s failed; dispatcher continues", type(source).__name__)
            # Leave state discovery to the source itself — its state
            # attribute should already reflect FAILED by now.
            if source.state is SourceState.RUNNING:
                # Keep the telemetry honest even when the source forgets.
                source.state = SourceState.FAILED

    async def _process(self, payload: dict[str, float]) -> None:
        if not self._deadman.accepts_action():
            # Reset the validator's rate clock so the first frame after
            # re-engage isn't rejected as a burst against a stale
            # timestamp from the pre-disarm epoch.
            self._validator.reset()
            self.dropped_deadman += 1
            return
        now_ms = self._clock() * 1000.0
        try:
            sanitised = self._validator.check({"values": payload}, now_ms=now_ms)
        except ActionValidationError as exc:
            logger.debug("action dropped (%s): %s", exc.code, exc.details)
            self.dropped_validation += 1
            return
        try:
            await self._robot_manager.send_action(self._robot_id, sanitised)
        except Exception:
            logger.exception("robot_manager.send_action failed")
            self.dropped_validation += 1
            return
        self.forwarded += 1
