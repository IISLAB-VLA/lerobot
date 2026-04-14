"""Teleoperation WebSocket primitives (task #11 scaffolding).

This package holds the transport-agnostic pieces of the teleop channel so
they can be unit-tested without a live robot or a WebSocket. The actual
``/ws/teleop`` handler wiring lands in task #11 once the robot/camera
adapter layer (task #6) is available.

Modules:

* :mod:`.protocol` — frame envelope, client/server message types, parsing.
* :mod:`.deadman` — four-state deadman switch state machine.
* :mod:`.validator` — :class:`ActionSpec` + :class:`ActionValidator`
  (joint bounds, value finiteness, send rate limit).

The WS handler itself will wire these together around a
``RobotManagerProtocol.send_action(robot_id, action: dict[str, float])``
call once task #6 ships the adapter.
"""

from lerobot.dashboard.teleop.deadman import (
    ARMING_DELAY_MS,
    HEARTBEAT_TIMEOUT_MS,
    DeadmanState,
    DeadmanStateMachine,
)
from lerobot.dashboard.teleop.dispatcher import TeleopDispatcher
from lerobot.dashboard.teleop.protocol import (
    ClientFrame,
    ClientFrameType,
    GamepadEvent,
    KeyboardEvent,
    MouseEvent,
    ProtocolError,
    ServerFrame,
    ServerFrameType,
    TeleopEvent,
    TeleopEventKind,
    parse_teleop_event,
)
from lerobot.dashboard.teleop.validator import (
    ActionSpec,
    ActionValidationError,
    ActionValidator,
)

__all__ = [
    "ARMING_DELAY_MS",
    "ActionSpec",
    "ActionValidationError",
    "ActionValidator",
    "ClientFrame",
    "ClientFrameType",
    "DeadmanState",
    "DeadmanStateMachine",
    "GamepadEvent",
    "HEARTBEAT_TIMEOUT_MS",
    "KeyboardEvent",
    "MouseEvent",
    "ProtocolError",
    "ServerFrame",
    "ServerFrameType",
    "TeleopDispatcher",
    "TeleopEvent",
    "TeleopEventKind",
    "parse_teleop_event",
]
