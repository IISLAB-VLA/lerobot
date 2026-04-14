"""Unit tests for the transport-agnostic teleop scaffolding (task #11 prep).

The real ``/ws/teleop`` handler is gated on task #6 (robot/camera adapter).
These tests exercise the pieces that don't need a live robot: the frame
envelope parser, the deadman state machine, and the action validator.
"""

from __future__ import annotations

import pytest

from lerobot.dashboard.teleop import (
    ARMING_DELAY_MS,
    HEARTBEAT_TIMEOUT_MS,
    ActionSpec,
    ActionValidationError,
    ActionValidator,
    ClientFrame,
    ClientFrameType,
    DeadmanState,
    DeadmanStateMachine,
    GamepadEvent,
    KeyboardEvent,
    MouseEvent,
    ProtocolError,
    ServerFrame,
    ServerFrameType,
    TeleopEventKind,
    parse_teleop_event,
)


# ---------------------------------------------------------------------------
# protocol.py
# ---------------------------------------------------------------------------


def test_client_frame_from_json_accepts_valid_frame():
    frame = ClientFrame.from_json(
        {
            "seq": 1,
            "ts_client_ms": 1_712_345_678_901,
            "type": "action",
            "payload": {"values": {"j1": 0.1, "j2": -0.2}},
        }
    )
    assert frame.type is ClientFrameType.ACTION
    assert frame.payload["values"]["j1"] == 0.1


def test_client_frame_missing_required_fields_raises():
    with pytest.raises(ProtocolError):
        ClientFrame.from_json({"seq": 1, "type": "heartbeat"})


def test_client_frame_rejects_bool_seq():
    # bool is-a int in Python; we reject it explicitly so a buggy client
    # can't smuggle True into a seq slot and silently pass.
    with pytest.raises(ProtocolError):
        ClientFrame.from_json(
            {"seq": True, "ts_client_ms": 0, "type": "heartbeat", "payload": {}}
        )


def test_client_frame_unknown_type_raises():
    with pytest.raises(ProtocolError):
        ClientFrame.from_json(
            {"seq": 1, "ts_client_ms": 0, "type": "panic", "payload": {}}
        )


def test_parse_teleop_event_decodes_every_kind():
    kb = parse_teleop_event({"kind": "keyboard", "key": "w", "pressed": True})
    assert isinstance(kb, KeyboardEvent)
    assert kb.kind is TeleopEventKind.KEYBOARD
    assert (kb.key, kb.pressed) == ("w", True)

    mouse = parse_teleop_event({"kind": "mouse", "dx": 1.5, "dy": -2.0, "buttons": 1})
    assert isinstance(mouse, MouseEvent)
    assert (mouse.dx, mouse.dy, mouse.buttons) == (1.5, -2.0, 1)

    pad = parse_teleop_event(
        {"kind": "gamepad", "axes": [0.1, -0.2], "buttons": [False, True]}
    )
    assert isinstance(pad, GamepadEvent)
    assert pad.axes == (0.1, -0.2)
    assert pad.buttons == (False, True)


def test_parse_teleop_event_missing_discriminator_raises():
    with pytest.raises(ProtocolError):
        parse_teleop_event({"key": "w", "pressed": True})


def test_parse_teleop_event_unknown_kind_raises():
    with pytest.raises(ProtocolError):
        parse_teleop_event({"kind": "imu", "values": [0.0]})


def test_parse_teleop_event_malformed_keyboard_raises():
    with pytest.raises(ProtocolError):
        parse_teleop_event({"kind": "keyboard", "pressed": True})  # missing 'key'


def test_server_frame_to_json_is_json_safe():
    frame = ServerFrame(
        seq=5,
        ts_server_ms=100,
        type=ServerFrameType.STATE,
        payload={"state": "engaged"},
    )
    blob = frame.to_json()
    assert blob == {"seq": 5, "ts_server_ms": 100, "type": "state", "payload": {"state": "engaged"}}


# ---------------------------------------------------------------------------
# deadman.py
# ---------------------------------------------------------------------------


def test_deadman_press_then_tick_engages_after_delay():
    sm = DeadmanStateMachine()
    assert sm.press(0.0) is DeadmanState.ARMING
    # Still arming just before the delay elapses.
    assert sm.tick(ARMING_DELAY_MS - 1) is DeadmanState.ARMING
    # Transitions to ENGAGED at the boundary.
    assert sm.tick(ARMING_DELAY_MS) is DeadmanState.ENGAGED
    assert sm.accepts_action()


def test_deadman_release_returns_to_idle_from_any_state():
    sm = DeadmanStateMachine()
    sm.press(0.0)
    sm.tick(ARMING_DELAY_MS)
    assert sm.state is DeadmanState.ENGAGED
    assert sm.release(ARMING_DELAY_MS + 1) is DeadmanState.IDLE
    assert not sm.accepts_action()


def test_deadman_heartbeat_timeout_latches_lockout():
    sm = DeadmanStateMachine()
    sm.press(0.0)
    sm.tick(ARMING_DELAY_MS)
    sm.heartbeat(ARMING_DELAY_MS)
    # No heartbeat for longer than the timeout -> LOCKOUT.
    state = sm.tick(ARMING_DELAY_MS + HEARTBEAT_TIMEOUT_MS + 1)
    assert state is DeadmanState.LOCKOUT
    # Pressing again does NOT leave lockout; release is required first.
    assert sm.press(ARMING_DELAY_MS + HEARTBEAT_TIMEOUT_MS + 2) is DeadmanState.LOCKOUT
    assert sm.release(ARMING_DELAY_MS + HEARTBEAT_TIMEOUT_MS + 3) is DeadmanState.IDLE
    # Fresh engage cycle after the acknowledgement.
    sm.press(ARMING_DELAY_MS + HEARTBEAT_TIMEOUT_MS + 4)
    assert sm.tick(ARMING_DELAY_MS * 2 + HEARTBEAT_TIMEOUT_MS + 5) is DeadmanState.ENGAGED


def test_deadman_fault_latches_lockout_and_clears_arming_timer():
    sm = DeadmanStateMachine()
    sm.press(0.0)
    sm.fault("e_stop", 10.0)
    assert sm.state is DeadmanState.LOCKOUT
    # Ticking past the arming delay after a fault must not re-engage.
    assert sm.tick(ARMING_DELAY_MS + 10) is DeadmanState.LOCKOUT


# ---------------------------------------------------------------------------
# validator.py
# ---------------------------------------------------------------------------


def _spec(rate_hz: float = 100.0) -> ActionSpec:
    return ActionSpec(
        joint_names=("j1", "j2"),
        low=(-1.0, -0.5),
        high=(1.0, 0.5),
        rate_limit_hz=rate_hz,
    )


def test_action_validator_accepts_well_formed_payload():
    v = ActionValidator(_spec())
    out = v.check({"values": {"j1": 0.1, "j2": -0.2}}, now_ms=0.0)
    assert out == {"j1": 0.1, "j2": -0.2}


def test_action_validator_accepts_flat_payload_for_compat():
    v = ActionValidator(_spec())
    out = v.check({"j1": 0.1, "j2": -0.2}, now_ms=0.0)
    assert out == {"j1": 0.1, "j2": -0.2}


def test_action_validator_rejects_missing_joint():
    v = ActionValidator(_spec())
    with pytest.raises(ActionValidationError) as ei:
        v.check({"values": {"j1": 0.0}}, now_ms=0.0)
    assert ei.value.code == "joint_mismatch"
    assert "j2" in ei.value.details["missing"]


def test_action_validator_rejects_extra_joint():
    v = ActionValidator(_spec())
    with pytest.raises(ActionValidationError) as ei:
        v.check({"values": {"j1": 0.0, "j2": 0.0, "j3": 0.0}}, now_ms=0.0)
    assert ei.value.code == "joint_mismatch"
    assert "j3" in ei.value.details["extra"]


def test_action_validator_rejects_out_of_range():
    v = ActionValidator(_spec())
    with pytest.raises(ActionValidationError) as ei:
        v.check({"values": {"j1": 0.0, "j2": 1.0}}, now_ms=0.0)
    assert ei.value.code == "out_of_range"
    assert ei.value.details["joint"] == "j2"


def test_action_validator_rejects_nan_and_bool():
    v = ActionValidator(_spec())
    with pytest.raises(ActionValidationError) as ei:
        v.check({"values": {"j1": float("nan"), "j2": 0.0}}, now_ms=0.0)
    assert ei.value.code == "invalid_value"
    with pytest.raises(ActionValidationError):
        v.check({"values": {"j1": True, "j2": 0.0}}, now_ms=0.0)


def test_action_validator_rate_limits_excess_sends():
    v = ActionValidator(_spec(rate_hz=100.0))  # min_dt_ms=10, slack 0.8 -> 8ms
    v.check({"values": {"j1": 0.0, "j2": 0.0}}, now_ms=0.0)
    # 5 ms later is too fast.
    with pytest.raises(ActionValidationError) as ei:
        v.check({"values": {"j1": 0.0, "j2": 0.0}}, now_ms=5.0)
    assert ei.value.code == "rate_limit"
    # 10 ms later is fine (at the nominal period).
    v.check({"values": {"j1": 0.0, "j2": 0.0}}, now_ms=10.0)


def test_action_spec_rejects_bad_bounds():
    with pytest.raises(ValueError):
        ActionSpec(joint_names=("j1",), low=(1.0,), high=(0.0,), rate_limit_hz=100.0)
    with pytest.raises(ValueError):
        ActionSpec(joint_names=("j1", "j1"), low=(0.0, 0.0), high=(1.0, 1.0), rate_limit_hz=100.0)
    with pytest.raises(ValueError):
        ActionSpec(joint_names=("j1",), low=(0.0,), high=(1.0,), rate_limit_hz=0.0)
