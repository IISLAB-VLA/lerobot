// Calibration wizard E2E — runs in fake_devices mode against the real
// CalibrationController. Joint feedback panel assertions are deferred until
// task #21 (uncalibrated raw motor read path) lands; for now we verify the
// wizard's structural flow: idle → start → 5 SO-101 steps → done → redirect.

import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

const SO101_STEP_PROMPTS = [
  /Move the arm to the home pose/i,
  /Sweep each joint through its full range/i,
  /Close the gripper/i,
  /Open the gripper/i,
  /Verify joint angles/i,
];

async function deleteAllRobotsAndCameras(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

async function seedConnectedRobot(request: APIRequestContext, name: string): Promise<string> {
  const robotRes = await request.post("/api/robots", {
    data: {
      name,
      robot_type: "so101_follower",
      connection: { kind: "serial", port: "/dev/fake-acm" },
      cameras: [],
    },
  });
  expect(robotRes.ok(), `POST /api/robots: ${robotRes.status()}`).toBe(true);
  const robot = await robotRes.json();
  const connectRes = await request.post(`/api/robots/${robot.id}/connect`);
  expect(connectRes.ok(), `connect: ${connectRes.status()} ${await connectRes.text()}`).toBe(true);
  return robot.id;
}

test.describe("Calibration wizard", () => {
  test.beforeEach(async ({ request }) => {
    await deleteAllRobotsAndCameras(request);
  });

  test("SO-101 wizard walks 5 steps and redirects to detail on done", async ({
    page,
    request,
    captureFullPage,
  }) => {
    const robotId = await seedConnectedRobot(request, "Calibrate Bot");

    await page.goto(`/#/robots/${robotId}/calibrate`);
    await expect(page.getByRole("heading", { level: 1, name: /Calibrate Calibrate Bot/i })).toBeVisible();

    // IdleView → start
    await page.getByRole("button", { name: /Start calibration/i }).click();
    await captureFullPage(page, "04_calibrate_step1");

    // Walk through every step. The Next button is disabled until the server
    // says awaiting_input, so toBeEnabled implicitly waits for the per-step
    // prompt to land. Pin to the H2 prompt heading so the longer instruction
    // paragraph (which often contains the same phrase) does not match too.
    const next = page.getByRole("button", { name: /^Next$/ });
    for (let i = 0; i < SO101_STEP_PROMPTS.length; i += 1) {
      await expect(
        page.getByRole("heading", { level: 2, name: SO101_STEP_PROMPTS[i] }),
      ).toBeVisible({ timeout: 15_000 });
      await expect(next).toBeEnabled({ timeout: 15_000 });
      await next.click();
    }

    // DoneView appears and auto-redirects to /robots/:id after ~2s.
    await expect(page.getByText(/Calibration complete/i)).toBeVisible({ timeout: 15_000 });
    await captureFullPage(page, "04_calibrate_done");

    await expect(page).toHaveURL(new RegExp(`#/robots/${robotId}$`), { timeout: 10_000 });

    await request.post(`/api/robots/${robotId}/disconnect`);
  });

  test("Cancel button aborts an active session and surfaces the idle state again", async ({
    page,
    request,
  }) => {
    const robotId = await seedConnectedRobot(request, "Cancel Bot");

    await page.goto(`/#/robots/${robotId}/calibrate`);
    await page.getByRole("button", { name: /Start calibration/i }).click();

    // Wait until the first step prompt is rendered before cancelling.
    await expect(
      page.getByRole("heading", { level: 2, name: SO101_STEP_PROMPTS[0] }),
    ).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: /^Cancel$/ }).click();

    // Either the wizard returns to idle (Start calibration) or the server
    // sends a done event with a non-ok result. Both signal the cancel was
    // honoured; assert one of them surfaces.
    const cancelled = page
      .getByRole("button", { name: /Start calibration/i })
      .or(page.getByText(/Calibration (cancelled|error|ended)/i));
    await expect(cancelled.first()).toBeVisible({ timeout: 15_000 });

    await request.post(`/api/robots/${robotId}/disconnect`);
  });
});
