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

# or directly
cd tests/dashboard/e2e && npm test
```

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
