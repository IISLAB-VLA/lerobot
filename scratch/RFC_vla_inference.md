# RFC — Dashboard VLA Inference Backend (Task #14)

Status: **draft** — awaiting review from team-lead, robotics-integrator-3, frontend-architect-3.
Author: backend-architect-3.

## Goal

Let a dashboard operator load a LeRobot-compatible policy (local HF cache
or `~/.cache/huggingface/hub/models--*`), bind it to a connected robot +
cameras, and start a live inference loop. The loop streams observation
→ policy.select_action → robot_manager.send_action at the policy's fps,
optionally accepting natural-language commands (pi0 / smolvla / xvla).
`dry_run=True` disables send_action so the operator can inspect a policy
before letting it drive hardware.

## Non-goals

- Training / fine-tuning from the dashboard (out of scope).
- Multi-policy composition or scheduling.
- Remote policy serving (HTTP policy endpoint); stays local for the MVP.
- Benchmarking (separate Task #16).

## User flow

1. Operator goes to the Robot detail page → "Run policy" tab.
2. Dropdown lists local cached policies discovered from
   `HF_LEROBOT_HOME / HF_HOME / ~/.cache/huggingface/hub` (Task #14 backend
   scan). Pinned metadata: `policy_type`, `repo_id`, `num_parameters`,
   `last_modified`, required `observation_features` / `action_features`
   for compatibility hinting.
3. Operator picks one, adjusts `fps` (default = policy config's fps),
   toggles `dry_run`, types a task description (e.g. "pick up the red
   cube"). Confirm → 202 + `{session_id}`.
4. Live WS stream shows: `step, action, latency_ms, text`. Optional image
   hash so the UI can flag drift vs. the recorder's captured frames.
5. `POST /api/inference/{id}/command` updates the natural-language
   instruction mid-session (only for policies that accept text).
6. Stop → cleanup. Sessions are discarded (no dataset side-effect).

## Architecture (reuses recorder dual-pull pattern)

```
┌───────────┐      ┌────────────────────┐     ┌───────────────┐
│  /api/inf │◄──── │ InferenceService   │ ◄── │ robot_manager │
│  + /ws    │      │  per-session task  │     │ camera_manager│
└───────────┘      │   read_observation │     └───────────────┘
                   │    + subscribe     │
                   │         ↓          │
                   │  DataProcessor     │
                   │    ↓ policy        │
                   │  policy.select_    │
                   │    action          │
                   │    ↓ postproc      │
                   │  send_action       │
                   └────────────────────┘
```

Direct analog of `RecorderService`: capture loop per session, dual-pull
observation + frames, then pipe through the policy rather than the
dataset writer.

## Surface

### REST

```
GET  /api/policies                        # list cached policies
GET  /api/policies/{repo_id}              # details for one
POST /api/inference                       # start session
GET  /api/inference                       # list sessions
GET  /api/inference/{id}
POST /api/inference/{id}/stop             # body {}; terminal
POST /api/inference/{id}/command          # body {"text": "new instruction"}
WS   /ws/inference/{id}                   # live step stream
```

#### `POST /api/inference` payload

```json
{
  "robot_id": "<UUID>",
  "repo_id": "lerobot/smolvla_pick_base",
  "fps": 30,
  "task_description": "pick up the red cube",
  "dry_run": false
}
```

Validation:

- Robot must be `is_connected=True`. Cameras referenced by the robot
  entry must be known (auto-open on session start, same as recorder).
- `repo_id` must exist in the scanned cache and match a known policy
  type. Unknown → 404.
- `fps` in `[1, 240]`. Defaults to the policy config's training fps if
  omitted.
- Only one active session per `robot_id` (same rule as recorder).

Response: `InferenceSession` with `status = "starting" / "running"`.

### WebSocket payloads

Server → client, ~policy-fps cadence:

```json
{
  "type": "step",
  "step": 123,
  "action": [0.0, 1.57, -0.3, 0.4],
  "latency_ms": 18.2,
  "image_hash": "...sha16...",
  "text": "pick up the red cube"
}
```

Terminal:

```json
{"type": "stopped", "reason": "client", "steps": 4200}
{"type": "error", "message": "OOM in policy.forward"}
```

## Internals

### `services/inference.py`

```python
class PolicyDescriptor(BaseModel):
    repo_id: str
    policy_type: str
    root: Path
    num_parameters: int | None
    last_modified: datetime
    observation_features: dict[str, Any] | None
    action_features: dict[str, Any] | None
    supports_language: bool  # pi0 / smolvla / xvla

def scan_hf_cache() -> list[PolicyDescriptor]: ...
def load_policy(repo_id: str) -> PreTrainedPolicy: ...
    # Uses get_policy_class + PreTrainedPolicy.from_pretrained
    # Caches the loaded policy in an LRU so repeated start/stop doesn't
    # rebuild the model weights. Max 2 resident policies by default.

class InferenceSession(BaseModel):
    id: UUID
    robot_id: UUID
    repo_id: str
    fps: int
    dry_run: bool
    task_description: str
    status: Literal["starting","running","stopping","stopped","failed"]
    step: int
    started_at: datetime
    stopped_at: datetime | None
    error: str | None
    last_latency_ms: float | None

class InferenceService:
    def __init__(self, registry, robot_manager, camera_manager): ...
    async def start(req: StartRequest) -> InferenceSession
    async def stop(session_id) -> InferenceSession
    async def list()
    async def get(session_id)
    async def set_command(session_id, text)   # mid-session language swap
    async def subscribe(session_id) -> AsyncIterator[StepEvent]
    async def close()                         # shutdown hook
```

### Capture / inference loop

```python
async def _run(session):
    policy = await asyncio.to_thread(load_policy, session.repo_id)
    policy.eval()
    period = 1.0 / session.fps
    while session.status == "running":
        tick = time.monotonic()
        obs = await self._robot_manager.read_observation(session.robot_id)
        for cam in session.cameras:
            frame = await wait_for(anext(iters[cam.id]), timeout=period*3)
            obs[f"observation.images.{cam.name}"] = frame
        batch = prepare_batch(obs, task_description=session.task_description)
        t0 = time.monotonic()
        action_tensor = await asyncio.to_thread(policy.select_action, batch)
        latency_ms = (time.monotonic() - t0) * 1000
        action_dict = self._tensor_to_action_dict(action_tensor)
        if not session.dry_run:
            await self._robot_manager.send_action(session.robot_id, action_dict)
        await self._emit(session, StepEvent(step=session.step, ..., latency_ms=latency_ms))
        session.step += 1
        await asyncio.sleep(max(0.0, period - (time.monotonic() - tick)))
```

`prepare_batch` runs the policy's `preprocessor` (if present on the
loaded Policy) and converts numpy → torch tensors on the policy's device.
`_tensor_to_action_dict` uses `policy.action_features` names to build the
dict that `RobotManagerProtocol.send_action` expects.

### HF cache scanning

`huggingface_hub.scan_cache_dir()` enumerates cached repos; filter to
entries with a `config.json` that matches one of the LeRobot policy
types via `get_policy_class`. Fallback heuristic: `policy_type` key in
the model config. Scanning is O(number of cached repos) and runs on a
worker thread; cached for 60s between invocations.

### Natural-language command path

- `set_command(session_id, text)` updates the session's
  `task_description`; the next batch carries the new string.
- Policies that don't accept language ignore the field (the dashboard UI
  hides the command box for them — derived from
  `PolicyDescriptor.supports_language`).

## Open questions

1. **Where does the policy run?** Default to `DEFAULT_DEVICE`
   (`lerobot.utils.utils.get_safe_torch_device`) inherited from the
   environment. Expose a `device` field in `PolicyDescriptor` for the UI
   but not as a start-request knob? Recommend yes — autoselect, hide the
   option for MVP.
2. **Processor pipeline recovery**. `PreTrainedPolicy.from_pretrained`
   already reloads the processor. Assume it's present; if not, fall back
   to a no-op identity pipeline and warn.
3. **Partial image set**. If one camera of a multi-camera policy is
   offline, treat as a drop (skip this tick) or pad with the last good
   frame? Recommend **drop + warn** for MVP; last-good fallback opens
   drift risk.
4. **Frontend polling**. Does the UI refresh the policy list on every
   page load or via a manual "Refresh cache" button? Lean toward auto on
   mount + manual refresh — cache scan is fast (<100ms typical).
5. **dry_run audit trail**. Do we want to persist the dry-run action
   stream to disk for later playback? Not in MVP; frontend can just log
   to browser-local storage.

## Phasing

- **Phase 1a** (this commit chain): `scan_hf_cache` + `load_policy` LRU
  - `InferenceService` scaffold + capture loop + `test_inference.py`
    (happy path, dry-run, cache scan, unknown repo 404).
- **Phase 1b**: `api/inference.py` + `/ws/inference/{id}` + `DI wiring`
  - TestClient integration test.
- **Phase 2** (frontend-architect-3): policy dropdown, live telemetry,
  command box, dry-run toggle.

## Files touched

- `src/lerobot/dashboard/services/inference.py` — new, ~350 lines.
- `src/lerobot/dashboard/api/inference.py` — new, ~120 lines.
- `src/lerobot/dashboard/api/policies.py` — new, ~60 lines (GET /list, /detail).
- `src/lerobot/dashboard/api/router.py` — include policies + inference routers.
- `src/lerobot/dashboard/ws/router.py` — `/ws/inference/{id}` endpoint.
- `src/lerobot/dashboard/core/state.py` — `AppState.inference: InferenceService | None`.
- `src/lerobot/dashboard/app.py` — wire service, close() in lifespan.
- `src/lerobot/dashboard/api/_deps.py` — `get_inference`, `map_inference_error`.
- `tests/dashboard/test_inference.py` — new, 12+ cases.
- `tests/dashboard/test_inference_api.py` — new, 8+ cases.

Total ≈ 700 lines. Split into two commits (scaffold; API).
