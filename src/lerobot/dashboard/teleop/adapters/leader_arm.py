"""Leader-arm input source: poll a lerobot ``Teleoperator`` in a thread.

The concrete ``lerobot.teleoperators.Teleoperator`` subclasses expose a
synchronous ``get_action()`` that typically drives a motor bus — we
can't block the event loop on it. This source wraps the call in
:func:`asyncio.to_thread` and reuses :class:`CallableLeaderSource` for
the actual polling loop and lifecycle semantics.

The wrapped type is declared here as a :class:`TeleoperatorPoller`
Protocol so tests and ``fake_devices`` mode don't need to pull in the
heavy ``lerobot.teleoperators`` module just to construct a source.
Duck-typing is sufficient; the real ``Teleoperator`` base already
satisfies the Protocol's three methods.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from lerobot.dashboard.teleop.adapters.base import (
    CallableLeaderSource,
    SourceState,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class TeleoperatorPoller(Protocol):
    """Duck-typed subset of ``lerobot.teleoperators.Teleoperator``."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def get_action(self) -> dict[str, Any]: ...


class LeaderArmEventSource:
    """Bridge a synchronous :class:`TeleoperatorPoller` into the dispatcher.

    Lifecycle:

    * ``start()`` calls ``teleop.connect()`` inside a worker thread, then
      boots the inner :class:`CallableLeaderSource`.
    * ``stop()`` cancels the polling task and calls
      ``teleop.disconnect()`` — both in worker threads.
    * ``events()`` simply delegates to the inner source.

    Every ``get_action`` call runs on a thread so the event loop stays
    responsive even when the motor bus blocks. The payload returned by
    ``get_action`` is coerced to ``dict[str, float]`` by
    :class:`CallableLeaderSource` so no additional massaging is needed.
    """

    def __init__(self, teleop: TeleoperatorPoller, *, rate_hz: float = 100.0) -> None:
        self._teleop = teleop
        self._inner = CallableLeaderSource(self._poll_once, rate_hz=rate_hz)

    @property
    def state(self) -> SourceState:
        return self._inner.state

    @state.setter
    def state(self, value: SourceState) -> None:
        # Keep compatibility with ``TeleopEventSource`` which declares
        # ``state`` as a plain attribute — the dispatcher may write to it
        # after a failure it detected outside the source.
        self._inner.state = value

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._teleop.connect)
        except Exception:
            self._inner.state = SourceState.FAILED
            raise
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()
        try:
            await asyncio.to_thread(self._teleop.disconnect)
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("teleoperator.disconnect raised; ignoring")

    def events(self) -> AsyncIterator[dict[str, float]]:
        return self._inner.events()

    async def _poll_once(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._teleop.get_action)
