"""Common teleop input-source protocol and a synthetic implementation.

The dispatcher (Task #11 WS handler, wired after Task #6) pulls events
off one or more :class:`TeleopEventSource` instances. The interface is
intentionally minimal — ``start`` / ``stop`` lifecycle plus an async
``events`` iterator yielding raw joint dicts. The dispatcher is where
deadman gating, validation, and coalescing live.

:class:`CallableLeaderSource` is a stand-in for the real
``LeaderArmEventSource`` that will wrap ``lerobot.Teleoperator`` once
Task #6 lands. It takes any zero-arg callable returning a
``dict[str, float]`` and runs it on a fixed interval, so tests can
drive the dispatcher without pulling hardware deps.
"""

from __future__ import annotations

import asyncio
import enum
from typing import AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable


class SourceState(str, enum.Enum):
    """Lifecycle states an adapter may report to the dispatcher."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@runtime_checkable
class TeleopEventSource(Protocol):
    """Unified input source contract consumed by the dispatcher.

    Implementations are async-context-like: ``start`` sets the source
    up (handshake, hardware connect, etc.), ``events`` yields
    ``dict[str, float]`` payloads, and ``stop`` tears things down.
    ``state`` must always reflect the current lifecycle bucket — the
    dispatcher reads it for telemetry frames.
    """

    state: SourceState

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def events(self) -> AsyncIterator[dict[str, float]]: ...


class CallableLeaderSource:
    """Synthetic leader-arm source driven by a user-supplied callable.

    The callable may be sync or async; both are awaited through a single
    code path so the dispatcher contract is identical to what the real
    hardware wrapper will look like.

    ``rate_hz`` is clamped on the low end — anything below 1 Hz is
    rejected because the validator's rate-limit slack would otherwise
    force the dispatcher to drop every event we produce.
    """

    def __init__(
        self,
        get_action: Callable[[], dict[str, float] | Awaitable[dict[str, float]]],
        *,
        rate_hz: float = 100.0,
    ) -> None:
        if rate_hz < 1.0:
            raise ValueError("rate_hz must be >= 1.0")
        self._get_action = get_action
        self._interval_s = 1.0 / rate_hz
        self._queue: asyncio.Queue[dict[str, float]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.state: SourceState = SourceState.STOPPED

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self.state = SourceState.STARTING
        self._task = asyncio.create_task(self._run(), name="CallableLeaderSource")
        # Hand control back so the task can flip to RUNNING on its first tick.
        await asyncio.sleep(0)

    async def stop(self) -> None:
        # Preserve a FAILED state across stop() so callers can observe the
        # reason the source died; only non-failed stops reset to STOPPED.
        was_failed = self.state is SourceState.FAILED
        self.state = SourceState.STOPPING
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                # The _run loop already flipped state to FAILED; surface
                # nothing further — stop() is the graceful-shutdown path.
                was_failed = True
            self._task = None
        self.state = SourceState.FAILED if was_failed else SourceState.STOPPED

    async def events(self) -> AsyncIterator[dict[str, float]]:
        while True:
            if self.state is SourceState.STOPPED and self._queue.empty():
                return
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=self._interval_s * 4)
            except asyncio.TimeoutError:
                if self.state in {SourceState.STOPPED, SourceState.FAILED}:
                    return
                continue
            yield payload

    async def _run(self) -> None:
        self.state = SourceState.RUNNING
        try:
            while True:
                payload = self._get_action()
                if asyncio.iscoroutine(payload):
                    payload = await payload
                if not isinstance(payload, dict):
                    raise TypeError(
                        f"get_action must return a dict, got {type(payload).__name__}"
                    )
                await self._queue.put({str(k): float(v) for k, v in payload.items()})
                await asyncio.sleep(self._interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state = SourceState.FAILED
            raise
