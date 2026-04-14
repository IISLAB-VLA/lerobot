"""Gamepad input source: passive queue fed by the WS handler.

Unlike :class:`LeaderArmEventSource` the gamepad source does no
polling of its own — browsers already sample the Web Gamepad API on
every animation frame and forward :class:`GamepadEvent` frames over
the teleop WebSocket. The source just maps each incoming event to a
``dict[str, float]`` action vector and yields it to the dispatcher.

Axis → joint mapping is intentionally pluggable. The default
:class:`LinearAxisMapping` covers the common "N gamepad axes map to N
robot joints at a fixed scale, with a deadzone" case and is enough for
MVP; advanced users can drop in their own mapping without touching the
source.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from lerobot.dashboard.teleop.adapters.base import SourceState
from lerobot.dashboard.teleop.protocol import GamepadEvent

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_SIZE = 8


@runtime_checkable
class GamepadMapping(Protocol):
    """Translate a :class:`GamepadEvent` to a joint action dict.

    Returning ``None`` signals "no command this frame" (e.g. inside
    deadzone) and the source will not forward anything.
    """

    def translate(self, event: GamepadEvent) -> dict[str, float] | None: ...


@dataclass(frozen=True)
class LinearAxisMapping:
    """Scale each axis linearly into a named joint, after a deadzone.

    ``axis_joints[i]`` is the joint name the i-th gamepad axis maps to.
    An axis that isn't assigned (``None``) is skipped. ``scale`` is a
    per-joint gain applied after the deadzone clip; ``deadzone`` applies
    symmetrically around zero. Values outside the resulting range must
    still pass the downstream :class:`ActionValidator`.
    """

    axis_joints: tuple[str | None, ...]
    scale: float = 1.0
    deadzone: float = 0.05

    def __post_init__(self) -> None:
        if not (0.0 <= self.deadzone < 1.0):
            raise ValueError("deadzone must be in [0, 1)")

    def translate(self, event: GamepadEvent) -> dict[str, float] | None:
        if not event.axes:
            return None
        action: dict[str, float] = {}
        for idx, raw in enumerate(event.axes):
            if idx >= len(self.axis_joints):
                break
            joint = self.axis_joints[idx]
            if joint is None:
                continue
            value = float(raw)
            if abs(value) < self.deadzone:
                value = 0.0
            else:
                # Rescale so the deadzone doesn't compress the
                # live-axis range — full-throw still produces ±1 * scale.
                sign = 1.0 if value > 0 else -1.0
                value = sign * (abs(value) - self.deadzone) / (1.0 - self.deadzone)
            action[joint] = value * self.scale
        return action or None


@dataclass
class GamepadEventSource:
    """Passive :class:`TeleopEventSource` driven by WS frames.

    The WS handler calls :meth:`feed` on every parsed
    :class:`GamepadEvent`; the source translates the event through its
    :class:`GamepadMapping` and buffers the resulting action on a
    bounded queue. On overflow we drop the *oldest* action so high-rate
    gamepads don't stall when the dispatcher momentarily falls behind.
    """

    mapping: GamepadMapping
    queue_size: int = _DEFAULT_QUEUE_SIZE
    state: SourceState = field(default=SourceState.STOPPED, init=False)
    _queue: asyncio.Queue[dict[str, float]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue = asyncio.Queue(maxsize=self.queue_size)

    async def start(self) -> None:
        self.state = SourceState.RUNNING

    async def stop(self) -> None:
        self.state = SourceState.STOPPED
        # Unblock any pending ``events()`` consumer; sending a sentinel is
        # unnecessary because the consumer loop checks ``state`` before
        # awaiting, but we clear the queue so stale commands don't leak
        # into the next session.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def feed(self, event: GamepadEvent) -> None:
        """WS-facing entry point. No-op when stopped."""
        if self.state is not SourceState.RUNNING:
            return
        action = self.mapping.translate(event)
        if action is None:
            return
        try:
            self._queue.put_nowait(action)
        except asyncio.QueueFull:
            # Drop oldest so new commands aren't stuck behind stale frames.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(action)
            except asyncio.QueueFull:  # pragma: no cover - would require concurrent feeds
                logger.debug("gamepad source queue saturated; dropping frame")

    async def events(self) -> AsyncIterator[dict[str, float]]:
        while self.state is SourceState.RUNNING:
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
