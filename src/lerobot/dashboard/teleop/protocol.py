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
from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    """Raised when a frame cannot be decoded against the envelope schema."""


class ClientFrameType(str, enum.Enum):
    ACTION = "action"
    DEADMAN = "deadman"
    HEARTBEAT = "heartbeat"
    MODE = "mode"


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
