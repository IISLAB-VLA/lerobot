# RFC — Task #16 benchmark visualization backend

Author: robotics-integrator-3 · Draft 2026-04-14

## Motivation

Users want to compare policy checkpoints against each other on the same
environment and visualize step-by-step behavior. The dashboard exposes
this as three pages: a list of past runs, a new-run form, and a run
detail view with side-by-side video, metric charts, and action heatmaps
(see the user brief copied into `TASK_BRIEF.md`).

This RFC covers the **backend**. FE owners (frontend-architect-3) read
the REST/WS contract here and build the three pages against it.

## Scope

Supported environments (MVP): **pusht**, **libero**. aloha / metaworld /
gym_manipulator come online as the env packages are wired in. A run
compares **one environment** against **N policy checkpoints** simultaneously
so the UI can render side-by-side videos and aggregate metrics.

Non-goals for MVP:

- Distributed / multi-worker rollouts. Phase 2 runs sequentially per
  policy on a single worker process; Phase 4 can parallelize.
- Training. This RFC only covers *evaluation* against pre-existing
  checkpoints; training lives in `lerobot-train`.
- Custom reward classifiers. The env's native reward is used.

## Phases

**Phase 1** (this PR) — Protocol + state machine + stub runner. No gym
rollout; the worker emits synthetic `step` events at the env's declared
`fps` so the FE page and Playwright specs can consume the WS contract
end-to-end. Tests: `tests/dashboard/test_benchmark.py` (13 tests).

**Phase 2** — Real gym rollout. Plug a runner that calls
`lerobot.envs.factory.make_env(cfg)` and `lerobot.policies.factory.get_policy_class(policy_type)` per checkpoint, steps the env for `episodes × max_steps`,
records observation (downscaled image), action, reward, done, policy
metadata. Writes a compact parquet under
`{storage}/benchmarks/{run_id}/trajectory.parquet` per episode per policy.

**Phase 3** — Preview videos. On episode end, encode the
downscaled observation frames into `{run_id}/{policy_slug}_ep{N}.mp4`
via `av` (h264, faststart). Emit a `preview_ready` event so the FE can
swap the placeholder for the encoded clip.

**Phase 4** — follow-up: policy_refs fan-out with env auto-reset, GPU
affinity, and metric aggregation across episodes.

## REST contract

All endpoints live under `/api/benchmarks`.

| Method | Path | Body | Success | Failure |
|---|---|---|---|---|
| GET | `/` | — | `200` `{benchmarks: [BenchmarkInfo]}` | — |
| POST | `/runs` | `{env_name, policy_refs[], episodes, seed?, task?}` | `202` BenchmarkRunSummary | `422` unknown env |
| GET | `/runs` | — | `200` `{runs: [BenchmarkRunSummary]}` (newest first) | — |
| GET | `/runs/{run_id}` | — | `200` BenchmarkRunSummary | `404` unknown run |
| POST | `/runs/{run_id}/cancel` | — | `202` BenchmarkRunSummary | `404` unknown run |

### `BenchmarkInfo`
```json
{"env_name": "pusht", "task": "PushT-v0", "fps": 10, "config_type": "PushtEnv"}
```

### `BenchmarkRunSummary`
```json
{
  "run_id": "run-ab12cd34",
  "env_name": "pusht",
  "task": "PushT-v0",
  "policy_refs": ["lerobot/diffusion_pusht", "lerobot/diffusion_pusht_aug"],
  "episodes": 10,
  "seed": 42,
  "status": "running",                       // queued | running | completed | cancelled | failed
  "progress": 0.47,                          // 0.0 .. 1.0
  "current_episode": 4,
  "steps_total": 120,
  "last_reward": 0.83,
  "started_at": "2026-04-14T...",
  "completed_at": null,
  "result": null,                             // {episodes_completed: N, ...} on completion
  "error": null,                              // {code, message} on failure
  "storage_dir": "/home/.../benchmarks/run-ab12cd34"
}
```

## WS contract — `/ws/benchmarks/{run_id}`

Read-only event stream, same pattern as calibration.

On connect the server replays the current `BenchmarkRunSummary` via a
`run` event so late subscribers don't need a REST round-trip. Terminal
runs (`completed / cancelled / failed`) replay `run` + `done` and then
close.

```ts
type BenchmarkServerEvent =
  | {type: "run"; summary: BenchmarkRunSummary}
  | {type: "step"; episode: number; step: number; reward: number;
     done: boolean; progress: number;
     observation_ref?: string;  // Phase 2: /resources/benchmarks/{run_id}/obs/e{N}_s{M}.jpg
     action?: Record<string, number>;           // Phase 2
     policy_slug?: string}                      // Phase 2: which policy is driving this tick
  | {type: "preview_ready"; policy_slug: string; episode: number;
     url: string}                               // Phase 3
  | {type: "done"; run_id: string; status: RunStatus;
     result?: object; error?: {code: string; message: string}}
```

C2S: `{type: "heartbeat"}` every 30s (same as calibration). Every other
C2S frame is ignored — user actions flow through REST.

## Storage layout

Per run the controller creates `{storage_dir}/benchmarks/{run_id}/` and
writes:

```
run_id/
  trajectory.parquet        # Phase 2: columns (policy_slug, episode, step, reward, done, action_<key>, obs_ref)
  observations/             # Phase 2: downscaled JPG frames (e.g. 256x256)
    e0_s0.jpg
    ...
  previews/                 # Phase 3: encoded mp4
    {policy_slug}_ep0.mp4
```

`BenchmarkRunSummary.storage_dir` exposes the absolute path so the
frontend (or recorder) can reference files directly via the existing
`/resources/...` mount.

## Open questions (review needed)

1. **Policy loading**: backend-architect-3's Task #14 exposes an HF
   cache scan + policy factory wrapper. Can `services/benchmark.py`
   reuse `HFPolicyLoader` (or equivalent) from VLA instead of duplicating
   the cache walk? Suggestion: extract the loader into
   `services/policy_loader.py` in Phase 2 so both VLA inference and
   benchmark rollouts share the same scan / validate code.
2. **Observation downscaling target size**: 256×256 default.
   Configurable via `BenchmarkStartRequest.preview_hw: (int, int)`? MVP
   hardcodes 256×256.
3. **Preview encoder**: `av` (via pyav) is already in the dashboard
   extra. VP8 vs H.264 — H.264 has wider browser support but needs
   `av` built with libx264. Default to the same codec the streaming
   track already uses (currently VP8 in `streaming/codecs.py`).
4. **Parallel policies**: Phase 4 only. For Phase 2 each policy runs
   sequentially — the UI still supports "side-by-side" by loading
   completed per-policy preview videos into separate tiles.
5. **Metric aggregation**: Phase 2 writes raw reward per step. The UI
   computes success rate / mean return client-side from the parquet
   fetched via a signed URL or `/api/benchmarks/runs/{run_id}/metrics`.
   Phase 2 adds the REST endpoint.

## Test strategy

- **Phase 1**: state machine + REST + WS tests with a stub runner. No
  gym. (done — 13 tests.)
- **Phase 2**: per-env smoke fixture — `pusht-v0` runs 1 episode with
  a random-action policy, asserts parquet schema. Gated by an `@env`
  marker so CI doesn't pull all env packages by default.
- **Phase 3**: preview encoding smoke — 3 synthetic frames → 1 mp4,
  assert file exists + ffprobe reports h264.
- Manual smoke on every phase: `lerobot-dashboard` server →
  `/api/benchmarks/runs` → WS stream → FE page renders.

## Timeline

- **Phase 1**: same PR as this RFC (ready to review).
- **Phase 2**: 1-2 days after RFC approval. Blocked on shared policy
  loader decision with backend-architect-3.
- **Phase 3**: 0.5 day after Phase 2.

## Open to changes

If team-lead prefers a different shape (e.g., inline the stub runner
in Phase 2 and ship REST-only here, or split per-policy runs into
sibling `run_id`s rather than one run with a list of policies), I'll
update before merging Phase 1.
