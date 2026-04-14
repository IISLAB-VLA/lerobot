"""Action-frame validator for the teleop channel.

The wire shape matches ``RobotManagerProtocol.send_action``:
``action: dict[str, float]`` keyed by joint name. :class:`ActionSpec`
describes the joint set, per-joint bounds, and maximum command rate;
:class:`ActionValidator` enforces the spec and returns a sanitised copy
ready to be forwarded to the robot manager.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


class ActionValidationError(ValueError):
    """Structured error so the WS handler can surface typed ``error`` frames."""

    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(f"{code}: {details}")
        self.code = code
        self.details = details


@dataclass(frozen=True)
class ActionSpec:
    """Shape + bounds + rate limit for a robot's action vector."""

    joint_names: tuple[str, ...]
    low: tuple[float, ...]
    high: tuple[float, ...]
    rate_limit_hz: float

    def __post_init__(self) -> None:
        if not (len(self.joint_names) == len(self.low) == len(self.high)):
            raise ValueError("joint_names / low / high must have the same length")
        if len(self.joint_names) == 0:
            raise ValueError("ActionSpec must declare at least one joint")
        if len(set(self.joint_names)) != len(self.joint_names):
            raise ValueError("joint_names must be unique")
        for name, lo, hi in zip(self.joint_names, self.low, self.high):
            if not (lo < hi):
                raise ValueError(f"joint {name!r}: low ({lo}) must be < high ({hi})")
        if self.rate_limit_hz <= 0:
            raise ValueError("rate_limit_hz must be positive")


class ActionValidator:
    """Per-session validator; stateful because rate-limiting is time-relative."""

    def __init__(self, spec: ActionSpec, *, rate_slack: float = 0.8) -> None:
        if not (0.0 < rate_slack <= 1.0):
            raise ValueError("rate_slack must be in (0, 1]")
        self._spec = spec
        self._min_dt_ms = 1000.0 / spec.rate_limit_hz
        self._rate_slack = rate_slack
        self._last_ts_ms: float | None = None

    @property
    def spec(self) -> ActionSpec:
        return self._spec

    def reset(self) -> None:
        """Forget the last-accepted timestamp (e.g. after a deadman re-engage)."""
        self._last_ts_ms = None

    def check(self, payload: Any, now_ms: float) -> dict[str, float]:
        values = self._extract_values(payload)
        self._check_joint_set(values)
        sanitised: dict[str, float] = {}
        for name, lo, hi in zip(self._spec.joint_names, self._spec.low, self._spec.high):
            raw = values[name]
            val = self._coerce_float(name, raw)
            if val < lo or val > hi:
                raise ActionValidationError(
                    "out_of_range", joint=name, value=val, low=lo, high=hi
                )
            sanitised[name] = val
        self._check_rate(now_ms)
        self._last_ts_ms = now_ms
        return sanitised

    def _extract_values(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ActionValidationError("wrong_shape", want="dict")
        values = payload.get("values")
        if values is None:
            # Accept flat payloads as a compat convenience for early clients,
            # but mandate dict shape.
            if all(k in payload for k in self._spec.joint_names):
                return dict(payload)
            raise ActionValidationError("missing_values")
        if not isinstance(values, dict):
            raise ActionValidationError("wrong_shape", want="dict", got=type(values).__name__)
        return values

    def _check_joint_set(self, values: dict[str, Any]) -> None:
        expected = set(self._spec.joint_names)
        got = set(values)
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        if missing or extra:
            raise ActionValidationError("joint_mismatch", missing=missing, extra=extra)

    @staticmethod
    def _coerce_float(name: str, raw: Any) -> float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ActionValidationError("invalid_value", joint=name, value=raw)
        val = float(raw)
        if math.isnan(val) or math.isinf(val):
            raise ActionValidationError("invalid_value", joint=name, value=raw)
        return val

    def _check_rate(self, now_ms: float) -> None:
        if self._last_ts_ms is None:
            return
        dt = now_ms - self._last_ts_ms
        min_allowed = self._min_dt_ms * self._rate_slack
        if dt < min_allowed:
            raise ActionValidationError(
                "rate_limit", dt_ms=dt, min_dt_ms=self._min_dt_ms
            )
