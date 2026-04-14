# RFC — Dashboard Dataset Recording Pipeline (Task #13)

Status: **draft** — awaiting review from team-lead, robotics-integrator-3, frontend-architect-3.
Author: backend-architect-3.

## Goal

Let a dashboard operator record a LeRobot-compatible dataset from a live robot
+ cameras + teleop session, entirely through the web UI. The recorded artifact
must load back into the existing `LeRobotDataset` consumer (train / replay /
HF push) without post-processing.

## Non-goals

- Editing / trimming existing datasets (separate task).
- Multi-robot recording in one session (out of scope; one robot per session).
- Pushing to HF Hub from the dashboard (operator does `lerobot-dataset push`
  afterwards). We only produce the local dataset directory.

## User flow

1. Operator connects a robot, cameras, and a teleop device via the existing
   Home / Robot-detail UI.
2. On the robot detail page, "Start recording" opens a modal:
   `{dataset_name, task_description, fps (default robot.fps or 30)}`.
3. Operator confirms → dashboard streams capture progress (frame count,
   duration, disk bytes) at 1 Hz until they stop.
4. On stop, operator picks "Save" (finalize episode + flush) or "Discard"
   (clear buffer, remove temp files).
5. Dataset appears under `storage_dir/datasets/{dataset_name}` with the same
   layout `lerobot-train` expects.

Episode-vs-session handling is **implicit** in this iteration: one recording
session == one episode. Multi-episode sessions come later.

## Surface

### REST

```
POST   /api/recordings                 # create session, returns 202 + {session_id}
GET    /api/recordings                 # list sessions (active + recent)
GET    /api/recordings/{id}            # session detail + status
POST   /api/recordings/{id}/stop       # body {save: bool}, returns 202 + summary
DELETE /api/recordings/{id}            # only allowed on stopped sessions (cleanup)
```

#### `POST /api/recordings` payload

```json
{
  "robot_id": "<UUID>",
  "dataset_name": "pick_place_pilot_v3",   // becomes repo_id segment
  "task_description": "move cube to center",
  "fps": 30,                                // optional, defaults to robot fps
  "use_videos": true                        // optional
}
```

Validation:
- `robot_id` must exist in Registry **and** be `online=True` via
  `RobotManagerProtocol.is_connected`.
- Every camera referenced by the robot entry must be `is_open=True` via
  `CameraManagerProtocol`.
- `dataset_name` is `[a-z0-9_-]{1,64}` (reject path separators, no
  normalization magic).
- `fps` in `[1, 240]`.
- Refuse if another recording is active for the same `robot_id`.

Response body:
```json
{
  "session_id": "<UUID>",
  "dataset_path": "/home/.../datasets/pick_place_pilot_v3",
  "fps": 30,
  "started_at": "2026-04-14T07:42:00Z",
  "robot_id": "<UUID>",
  "cameras": ["<UUID>", ...]
}
```

#### `POST /api/recordings/{id}/stop` payload

```json
{ "save": true }
```

Server flow when `save=true`:
1. Stop capture loop (cancel the async task).
2. `dataset.save_episode()` → video encode, parquet flush.
3. `dataset.finalize()` — idempotent.
4. Summary response: `{frames, duration_s, disk_bytes, episode_index}`.

Server flow when `save=false`:
1. Stop capture loop.
2. `dataset.clear_episode_buffer(delete_images=True)`.
3. Summary response with `frames=0, saved=false`.

### WebSocket

```
WS /ws/recordings/{id}
```

Server → client every 1 s while capturing:
```json
{
  "type": "progress",
  "frames_captured": 423,
  "duration_s": 14.1,
  "disk_bytes": 18421322,
  "drop_count": 0
}
```

Terminal messages:
```json
{"type": "stopped", "saved": true, "episode_index": 0}
{"type": "error",   "message": "camera <id> closed unexpectedly"}
```

Writers from the client (optional): `{"type": "mark"}` to inject a manual
episode step marker into metadata for replay. Not required for v1.

## Internals

### `services/recorder.py`

```
class RecordingSession:
    id: UUID
    robot_id: UUID
    dataset: LeRobotDataset
    fps: int
    status: Literal["starting", "recording", "stopping", "saved", "discarded", "failed"]
    frames_captured: int
    started_at: datetime
    stopped_at: datetime | None
    error: str | None
    # internal:
    _task: asyncio.Task
    _progress_subscribers: set[asyncio.Queue]  # WS listeners

class RecorderService:
    def __init__(self, registry, robot_mgr, camera_mgr, datasets_dir): ...
    async def start(payload) -> RecordingSession
    async def stop(session_id, save: bool) -> RecordingSessionSummary
    async def list() -> list[RecordingSession]
    async def get(session_id) -> RecordingSession
    async def subscribe(session_id) -> AsyncIterator[ProgressEvent]
```

Only one active session per `robot_id` (enforced in `start()` via a lock).

### Capture loop (one async task per session)

```python
async def _run(session: RecordingSession):
    period = 1.0 / session.fps
    frame_idx = 0
    # Open frame iterators for every camera in parallel.
    cam_iters = {
        cam.id: camera_mgr.subscribe(cam.id)
        for cam in cameras
    }
    try:
        while session.status == "recording":
            tick_start = time.monotonic()
            frame = await _collect_frame(session.robot_id, cam_iters)
            # frame = {
            #   "task": session.task_description,
            #   "observation.state": <robot joints>,
            #   "action": <last commanded action>,
            #   "observation.images.front": ndarray,
            #   ...
            # }
            session.dataset.add_frame(frame)
            session.frames_captured += 1
            frame_idx += 1
            # Throttle to fps; if we fall behind, count as a drop but don't block.
            elapsed = time.monotonic() - tick_start
            await asyncio.sleep(max(0.0, period - elapsed))
    except asyncio.CancelledError:
        return
    except Exception as exc:
        session.status = "failed"
        session.error = str(exc)
        raise
```

Where `_collect_frame`:
- Reads the latest robot observation via `robot_manager.read_observation`.
- Pulls the next frame from each camera iterator via `asyncio.wait_for(anext(it), timeout=period)` to avoid a stuck camera freezing the whole session. On timeout we record a drop and fall through.
- Returns a dict matching `dataset.features`.

### Feature schema resolution

A robot entry does **not** currently carry a `features` spec. To derive one:

- `observation.state` shape comes from `robot_manager.read_observation()` — we
  probe once at session start and cache the vector dimension. If the adapter
  returns `{}` (InMemory fallback), we fall back to a dummy 1-D zero vector so
  tests still exercise the recorder path.
- `action` dimension mirrors `observation.state` (follower joints) unless the
  adapter advertises `action_space_shape()` — added as a hint on Task #6 to
  improve over time, not a blocker.
- `observation.images.{camera.name}` one entry per `CameraEntry` attached to
  the robot; dtype `uint8`, shape `(H, W, 3)` pulled from the entry.

We record this resolved feature dict into the session's metadata so the Task
#6 adapter roadmap has a concrete target for `action_space_shape()`.

### Storage layout

```
<DashboardConfig.storage_dir>/datasets/<dataset_name>/
  meta/
  data/
  videos/
  ... (standard LeRobotDataset layout)
```

Override: `LEROBOT_DASHBOARD_DATASET_DIR` env var if the operator wants a
non-default root (backed by a CLI flag follow-up in task #2-like pattern).

### DI wiring

- `AppState.recorder: RecorderService | None` (new field).
- `create_app` instantiates it given `registry`, `robot_manager`,
  `camera_manager`, `config.dataset_dir`.
- Shutdown: `recorder.close()` cancels all active sessions, attempting
  `save_episode()` only on cleanly-stopped sessions; otherwise
  `clear_episode_buffer` to avoid partial parquet.

## Open questions (review please)

1. **Feature schema discovery** — should the robot adapter (Task #6) declare
   `action_space_shape()` / `observation_space_shape()` for this recorder, or
   is the probe-at-start approach acceptable? The probe is simpler but fragile
   if the robot is in an unusual state at session start.
2. **Multi-episode sessions** — leave explicit "episode break" out of v1 (one
   session = one episode), or bake it into the WS channel now
   (`{"type": "episode_break"}`)? Leaning toward v1 without it to keep UI
   simple.
3. **Streaming vs. image-writer** — `LeRobotDataset.create(..., streaming_encoding=True)`
   avoids the temp-image step but requires a working ffmpeg pipeline per
   camera. Default `streaming_encoding=False` is safer; expose as a session
   option later.
4. **Progress cadence** — 1 Hz is plenty for the UI but the server is doing
   the sampling anyway (e.g., on capture drops). Should we push on
   frame-event boundaries too (drop detected, video encode finished)? I'd
   push "drop" and "encode_complete" as additional event types alongside
   the 1 Hz progress heartbeat.
5. **fake_devices mode** — in `LEROBOT_DASHBOARD_FAKE_DEVICES=1` the
   InMemory managers still yield zero frames; the recorder should work end-
   to-end. Confirming this is acceptable for E2E tests so Playwright can
   record → save → inspect dataset listing without hardware.

## Phasing

- **Phase 1 (this RFC)** — REST + WS + RecorderService + capture loop +
  happy-path tests. No frontend work yet.
- **Phase 2** — Frontend modal + recording state on the Robot detail page
  (frontend-architect-3). Blocked on Phase 1 merge.
- **Phase 3** — Dataset browser in the dashboard (list existing datasets,
  preview a few frames). Out of scope for Task #13; new task if needed.

## Files touched (estimate)

- `src/lerobot/dashboard/services/recorder.py` — new, ~300 lines.
- `src/lerobot/dashboard/api/recordings.py` — new, ~150 lines.
- `src/lerobot/dashboard/api/router.py` — +1 line (include recordings router).
- `src/lerobot/dashboard/ws/recordings.py` — new, ~80 lines.
- `src/lerobot/dashboard/ws/router.py` — +1 line.
- `src/lerobot/dashboard/core/config.py` — +1 `dataset_dir` field, 1 env var.
- `src/lerobot/dashboard/core/state.py` — +1 `recorder` field.
- `src/lerobot/dashboard/api/_deps.py` — +1 `get_recorder` helper.
- `src/lerobot/dashboard/app.py` — wire RecorderService in `create_app`.
- `tests/dashboard/test_recorder.py` — new, 12+ cases.

Total ≈ 650 lines, single feature commit. I'd split it as:
1. `feat(dashboard): RecorderService scaffold + lifecycle` (service + tests).
2. `feat(dashboard): /api/recordings endpoints + WS progress` (surface).
