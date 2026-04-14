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
make dashboard-e2e

# or directly
cd tests/dashboard/e2e && npm test
```

## Visual regression

Per-spec screenshots land under `tests/dashboard/e2e/screenshots/`. Each major
screen is then fed to the `gemini-vision` skill for visual QA (layout, contrast,
intended feature placement). Issues are reported back to the frontend-architect
via SendMessage rather than committed as baselines (baselines are brittle to
minor intentional design changes).

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
