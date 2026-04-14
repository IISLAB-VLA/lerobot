"""Teleop input adapters (task #12 prep).

Every adapter exposes a uniform :class:`TeleopEventSource` so the
dispatcher can consume gamepad, leader-arm, and future modalities (VR
controllers, phone IMU, …) through the same code path. Concrete
hardware-backed sources (`LeaderArmEventSource`) wait on task #6 before
landing; this subpackage currently ships the transport-agnostic base and
a synthetic source suitable for unit tests.
"""

from lerobot.dashboard.teleop.adapters.base import (
    CallableLeaderSource,
    SourceState,
    TeleopEventSource,
)
from lerobot.dashboard.teleop.adapters.gamepad import (
    GamepadEventSource,
    GamepadMapping,
    LinearAxisMapping,
)
from lerobot.dashboard.teleop.adapters.leader_arm import (
    LeaderArmEventSource,
    TeleoperatorPoller,
)

__all__ = [
    "CallableLeaderSource",
    "GamepadEventSource",
    "GamepadMapping",
    "LeaderArmEventSource",
    "LinearAxisMapping",
    "SourceState",
    "TeleopEventSource",
    "TeleoperatorPoller",
]
