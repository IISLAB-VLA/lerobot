"""WebSocket message envelope for the dashboard teleop channel.

Both directions use a uniform shape — ``{seq, ts_*_ms, type, payload}`` —
so the browser and the server share a single parser. ``type`` enums are
stringly-typed on the wire (see :class:`ClientFrameType`, :class:`ServerFrameType`)
so the frontend can union-discriminate without looking up an integer map.

Per-type payload contracts (enforced by the handler, not by this module):

* ``action``    — ``{"values": {joint_name: float, ...}}``. Joint vector
  is a dict so the client can send partial frames for high-DOF robots;
  validation/rate-limiting lives in :mod:`.validator`.
* ``deadman``   — ``{"held": bool}``. Drives :class:`DeadmanStateMachine`.
* ``heartbeat`` — ``{}``. Keep-alive; 100 ms cadence recommended.
* ``mode``      — ``{"mode": "idle" | "teleop" | "replay"}``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Union


class ProtocolError(ValueError):
    """Raised when a frame cannot be decoded against the envelope schema."""


class TeleopEventKind(str, enum.Enum):
    """Discriminator for :data:`TeleopEvent`.

    These are the raw human-input events the dispatcher fans out to
    :class:`TeleopManagerProtocol.handle_input`. The ``action`` path
    (pre-sanitised joint vectors) stays on the envelope layer —
    ``ClientFrame(type=action)`` goes through :class:`ActionValidator`
    and :meth:`RobotManagerProtocol.send_action` directly without
    constructing a :class:`TeleopEvent`.
    """

    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    GAMEPAD = "gamepad"


@dataclass(frozen=True)
class KeyboardEvent:
    kind: TeleopEventKind = field(default=TeleopEventKind.KEYBOARD, init=False)
    key: str = ""
    pressed: bool = False


@dataclass(frozen=True)
class MouseEvent:
    kind: TeleopEventKind = field(default=TeleopEventKind.MOUSE, init=False)
    dx: float = 0.0
    dy: float = 0.0
    buttons: int = 0


@dataclass(frozen=True)
class GamepadEvent:
    kind: TeleopEventKind = field(default=TeleopEventKind.GAMEPAD, init=False)
    axes: tuple[float, ...] = ()
    buttons: tuple[bool, ...] = ()


TeleopEvent = Union[KeyboardEvent, MouseEvent, GamepadEvent]
"""Tagged-union type consumed by the dispatcher's input handler.

Always carries ``kind`` (a :class:`TeleopEventKind`) so downstream code
can ``match`` on event shape without instance-checks scattered through
the handler.
"""


def parse_teleop_event(payload: Any) -> TeleopEvent:
    """Decode one raw ``dict`` payload into a typed :data:`TeleopEvent`.

    Raises :class:`ProtocolError` when the discriminator is missing or
    the payload fields don't match the declared event shape.
    """
    if not isinstance(payload, dict):
        raise ProtocolError(f"event payload must be an object, got {type(payload).__name__}")
    raw_kind = payload.get("kind")
    if raw_kind is None:
        raise ProtocolError("event payload missing 'kind' discriminator")
    try:
        kind = TeleopEventKind(raw_kind)
    except ValueError as exc:
        raise ProtocolError(f"unknown teleop event kind: {raw_kind!r}") from exc
    try:
        if kind is TeleopEventKind.KEYBOARD:
            return KeyboardEvent(key=str(payload["key"]), pressed=bool(payload["pressed"]))
        if kind is TeleopEventKind.MOUSE:
            return MouseEvent(
                dx=float(payload.get("dx", 0.0)),
                dy=float(payload.get("dy", 0.0)),
                buttons=int(payload.get("buttons", 0)),
            )
        # gamepad
        axes = tuple(float(v) for v in payload.get("axes", ()))
        buttons = tuple(bool(v) for v in payload.get("buttons", ()))
        return GamepadEvent(axes=axes, buttons=buttons)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"malformed {kind.value} event: {exc}") from exc


class ClientFrameType(str, enum.Enum):
    ACTION = "action"
    DEADMAN = "deadman"
    HEARTBEAT = "heartbeat"
    MODE = "mode"
    INPUT = "input"


class ServerFrameType(str, enum.Enum):
    ACK = "ack"
    STATE = "state"
    ERROR = "error"
    TELEMETRY = "telemetry"


@dataclass(frozen=True)
class ClientFrame:
    """Validated browser → server frame."""

    seq: int
    ts_client_ms: int
    type: ClientFrameType
    payload: dict[str, Any]

    @classmethod
    def from_json(cls, raw: Any) -> "ClientFrame":
        if not isinstance(raw, dict):
            raise ProtocolError(f"frame must be an object, got {type(raw).__name__}")
        try:
            return cls(
                seq=_as_int(raw, "seq"),
                ts_client_ms=_as_int(raw, "ts_client_ms"),
                type=ClientFrameType(raw["type"]),
                payload=_as_payload(raw.get("payload")),
            )
        except KeyError as exc:
            raise ProtocolError(f"missing field: {exc.args[0]}") from exc
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc


@dataclass(frozen=True)
class ServerFrame:
    """Validated server → browser frame."""

    seq: int
    ts_server_ms: int
    type: ServerFrameType
    payload: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": int(self.seq),
            "ts_server_ms": int(self.ts_server_ms),
            "type": self.type.value,
            "payload": dict(self.payload),
        }


def _as_int(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        # bool is-a int in Python; reject explicitly.
        raise ProtocolError(f"field {key!r} must be int, got {type(value).__name__}")
    return value


def _as_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError("payload must be an object")
    return dict(value)
