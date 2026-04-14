import { defineConfig, devices } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.DASHBOARD_PORT ?? 18080);
const BASE_URL = process.env.DASHBOARD_BASE_URL ?? `http://localhost:${PORT}`;
// Isolate registry state per run so local ~/.cache/lerobot/dashboard stays clean
// and specs do not see leftover robots/cameras from a previous session.
const STORAGE_DIR =
  process.env.DASHBOARD_STORAGE_DIR ?? mkdtempSync(join(tmpdir(), "lerobot-dashboard-e2e-"));

export default defineConfig({
  testDir: "./specs",
  outputDir: "./test-results",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: [
    ["list"],
    ["html", { outputFolder: "./playwright-report", open: "never" }],
    ["json", { outputFile: "./test-results/results.json" }],
  ],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
  webServer: {
    command: `uv run lerobot-dashboard --host 127.0.0.1 --port ${PORT} --static-dir src/lerobot/dashboard/static --storage-dir ${STORAGE_DIR}`,
    url: `${BASE_URL}/api/health`,
    cwd: "../../../",
    env: {
      LEROBOT_DASHBOARD_FAKE_DEVICES: "1",
      LEROBOT_DASHBOARD_FAKE_POLICY: "1",
      PYTHONUNBUFFERED: "1",
    },
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    stdout: "pipe",
    stderr: "pipe",
  },
});
