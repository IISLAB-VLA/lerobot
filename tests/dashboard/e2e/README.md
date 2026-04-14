# LeRobot Dashboard E2E

Playwright-based end-to-end and visual regression suite for the `lerobot[dashboard]` web app.

## One-time setup

```bash
cd tests/dashboard/e2e
npm install
npx playwright install --with-deps chromium
```

The backend is spawned automatically by Playwright's `webServer` config:

```bash
uv run lerobot-dashboard --host 127.0.0.1 --port 18080
```

The spec suite requires `LEROBOT_DASHBOARD_FAKE_DEVICES=1` to serve deterministic
mock devices without real hardware (see Task #4 / robotics-integrator).

## Running

```bash
# from repo root
make dashboard-e2e            # rebuilds the Vite bundle first
make dashboard-e2e-nobuild    # skips the rebuild (fast inner loop)
make dashboard-e2e-hardware   # opt-in, real hardware, @hardware specs only

# or directly
cd tests/dashboard/e2e && npm test
```

## Hardware smoke (`@hardware`)

`specs/10_hardware_smoke.spec.ts` and any future `@hardware`-tagged test runs
only when `LEROBOT_DASHBOARD_E2E_HARDWARE=1` is set (Makefile target
`dashboard-e2e-hardware`). The default run uses `grepInvert: /@hardware/` so
these specs never execute in CI.

Prerequisites:

- SO-101 follower arm attached at `/dev/ttyACM0` (user must have permission
  to open the tty — check `groups` includes `dialout` on Linux).
- USB cameras at `/dev/video0` (belly), `/dev/video2` (top), `/dev/video4`
  (wrist). The spec uses the belly cam only; the extra ports are available
  for future multi-view cases.
- No other process holding the serial port or the `/dev/video0` node.

The smoke scenario walks device discovery → UI Add-Robot flow → manager
`POST /connect` → detail page deep-link → first WebRTC frame
(`HTMLVideoElement.readyState >= 2`) → `POST /disconnect`. It never calls
`pkill` — Playwright's `webServer` owns the backend lifecycle, and we
explicitly avoid `pkill -f lerobot-dashboard` because that pattern also
matches teammate agent command lines and has crashed the whole session
before.

## Visual regression

Per-spec screenshots land under `tests/dashboard/e2e/screenshots/` (gitignored).
Each major screen is fed to the `gemini-vision` skill for a non-binary layout /
contrast / feature-placement review. Issues are reported to frontend-architect
via SendMessage rather than committed as image baselines, which are brittle to
minor intentional design changes.

Captured screens and their intended assertions:

| File | Intent |
|------|--------|
| `00_home.png` | Home H1, Backend health card, /api/health label, status row readable. |
| `01_home.png` | Same as 00 but via a cold navigation (regression guard against route changes). |
| `02_robots_empty.png` | `/robots` empty state with "Add your first robot" CTA + top-right "Add robot". |
| `02_add_robot_step3.png` | AddRobotModal Step 3 with `No cameras yet` placeholder. |
| `02_robots_after_create.png` | Grid shows the just-created robot card (image, status dot, connection summary). |
| `03_robot_detail_cams.png` | Detail page with cameras: H1, Pause streams, LayoutSwitcher, stream tiles visible. |
| `03_robot_detail_no_cams.png` | Detail page with empty-state message + all multi-tile layouts disabled. |

To re-run a visual QA pass on the current screenshots, invoke `gemini-vision`
with a per-file transcription + layout-issue prompt. A recent pass (dash-qa
52173d6d) flagged only expected-empty regions (no robots yet / robot offline)
and the `text-muted-foreground` contrast band on Home, which frontend-architect
has since addressed in `bf9cfe25`.

## Layout

```
tests/dashboard/e2e/
├── fixtures/        # Playwright fixtures (screenshot helpers, mocks)
├── specs/           # *.spec.ts scenarios, numbered in execution order
├── screenshots/     # generated (gitignored)
├── playwright.config.ts
├── package.json
└── tsconfig.json
```
