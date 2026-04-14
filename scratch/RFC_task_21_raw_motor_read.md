# RFC: Uncalibrated Raw Motor Tick Read Path (Task #21)

**Status**: DRAFT
**Author**: backend-architect-3
**Stakeholders**: robotics-integrator-3 (hardware smoke), frontend-architect-3 (calibration UI)

---

## Problem

The calibration UI (`/ws/robots/{id}/calibrate`) streams `joint_feedback` events from
`calibration.py::_feedback_loop` at 10 Hz. The feedback loop calls
`robot_manager.read_observation(robot_id)`, which goes through
`SOFollower.get_observation()` → `bus.sync_read("Present_Position")` with **normalization
enabled**.

Normalization maps raw Feetech encoder ticks (0–4095) to degrees or percentage using the
calibration table (`Present_Position` ± homing offset ÷ range). The problem is:

1. **During calibration** the operator hasn't defined the calibration table yet — normalizing
   with a stale or absent table returns garbage values (or raises).
2. **robotics-integrator-3's hardware smoke** needs to verify motors are responding without
   running a full calibration first.
3. The calibration wizard UI needs to display "where the joint is right now" in raw ticks so
   the operator can move it to the target position and record it.

Currently `robot.connect(calibrate=False)` is called for the calibration session (so motors
do move), but `get_observation()` still calls `sync_read("Present_Position")` with the
default `normalize=True`, which silently returns empty or wrong values if the calibration
table is missing.

---

## Proposed Solution

### Layer 1 — `RobotManagerProtocol` (new method)

Add `read_raw_encoder_ticks` to the Protocol (and both concrete managers):

```python
async def read_raw_encoder_ticks(self, robot_id: UUID) -> dict[str, int]:
    """Read raw encoder tick values without calibration or normalization.

    Returns a mapping of ``{motor_name: raw_tick_value}`` using the motor
    names as registered on the bus (e.g. ``"shoulder_pan": 2048``).
    Returns an empty dict when the robot is offline, not a Feetech bus
    robot, or the raw read fails — callers should treat {} as "unavailable"
    rather than an error.
    """
```

**`InMemoryRobotManager`**: returns `{}` (no hardware).
**`LerobotRobotManager`**: calls `robot.bus.sync_read("Present_Position", normalize=False)`
wrapped in `asyncio.to_thread`. Falls back to `{}` on any exception.

### Layer 2 — `calibration.py::_feedback_loop` (extend WS event)

Extend `joint_feedback` WS event to include raw ticks alongside the existing `values` field:

```json
{
  "type": "joint_feedback",
  "session_id": "...",
  "step_id": "...",
  "values": {"shoulder_pan.pos": 180.0, ...},
  "raw_ticks": {"shoulder_pan": 2048, ...},
  "timestamp_ms": 1713100000000
}
```

- `values`: existing normalized read (may be empty/stale during calibration — unchanged)
- `raw_ticks`: new field from `read_raw_encoder_ticks()`; `{}` when unavailable
- **Backward compatible** — clients that ignore unknown fields are unaffected

### Layer 3 — `GET /api/robots/{id}/raw_encoder_ticks` (REST one-shot)

One-shot REST endpoint for hardware smoke tests (no need to open a WS session):

```
GET /api/robots/{id}/raw_encoder_ticks
→ {"robot_id": "...", "ticks": {"shoulder_pan": 2048, ...}, "timestamp_ms": ...}
```

Returns 200 with `ticks: {}` when robot is offline (not 404/503) — callers can distinguish
"robot offline" from "robot connected, zero motors" via `GET /api/robots/{id}/status`.

---

## Implementation Plan

### Phase 1 — Protocol + managers (no hardware dependency)

1. `robot_manager.py`: add `read_raw_encoder_ticks` to `RobotManagerProtocol` and
   `InMemoryRobotManager` (returns `{}`).
2. `robot_manager_impl.py`: implement `LerobotRobotManager.read_raw_encoder_ticks`:
   - Acquire lock, get slot
   - Access `robot.bus` via `getattr(robot, "bus", None)` (Feetech robots expose `.bus`;
     non-Feetech robots like UR return `None` → `{}`)
   - Call `bus.sync_read("Present_Position", normalize=False)` in thread
   - Strip motor suffix if needed (bus returns `{motor_id: int}`)
   - Fall back to `{}` on any exception (don't mark robot offline — raw read failure
     is softer than get_observation failure)
3. `api/robots.py`: add `GET /{robot_id}/raw_encoder_ticks` endpoint.
4. Unit tests: `test_robot_manager_impl.py` — stub bus, verify normalize=False called.

### Phase 2 — calibration WS integration

5. `calibration.py::_feedback_loop`: add parallel call to `read_raw_encoder_ticks`
   (fire-and-forget; populate `raw_ticks` in event, `{}` if unavailable).
6. Update `test_calibration.py`: `_FakeRobotManager` gets a `read_raw_encoder_ticks`
   stub; assert `raw_ticks` appears in WS events.

### Phase 3 — smoke test (hardware)

7. `tests/dashboard/test_smoke_robot.py` (new, `@pytest.mark.hardware`):
   - Requires real SO-101 at `/dev/ttyACM0`
   - `GET /api/robots/{id}/raw_encoder_ticks` returns non-empty dict with int values
   - Values change when the arm is manually moved

---

## Open Questions

1. **`bus.sync_read` motor name format**: `sync_read("Present_Position", normalize=False)`
   returns `{motor_name: int}` keyed by the motor's registered name string (same as in
   `motors: dict[str, MotorConfig]`). This matches what the calibration wizard UI expects.
   Confirmed from `bus.calibrate()` source (lines 791, 821, 827 in `motors_bus.py`).

2. **Non-Feetech robots (UR, Koch)**: `getattr(robot, "bus", None)` returns `None` for
   UR robots (they use urrtde, not a Feetech bus). These simply return `{}`.

3. **Race with `sync_read` normalization step**: `sync_read(normalize=False)` bypasses
   `_normalize()` but still calls `_decode_sign()`. This is fine — signed integers are
   still "raw ticks" in the sense that no calibration table is applied.

4. **Frontend consumption of `raw_ticks`**: frontend-architect-3 can use `raw_ticks` in
   the calibration step UI to show the current encoder position and highlight when it
   matches the target. The `values` field stays for backward compat.

---

## Files Changed

```
src/lerobot/dashboard/services/robot_manager.py          (Protocol + InMemory)
src/lerobot/dashboard/services/robot_manager_impl.py     (LerobotRobotManager)
src/lerobot/dashboard/api/robots.py                      (new endpoint)
src/lerobot/dashboard/services/calibration.py            (_feedback_loop)
tests/dashboard/test_robot_manager_impl.py               (new test)
tests/dashboard/test_calibration.py                      (extend)
tests/dashboard/test_smoke_robot.py                      (new, @hardware)
```

No changes to lerobot core motor bus API needed — `sync_read(normalize=False)` already
exists and is used by the calibration wizard internally.
