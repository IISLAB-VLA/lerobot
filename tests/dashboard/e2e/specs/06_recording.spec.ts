// Recording E2E — exercises the dataset capture flow end to end against
// fake_devices=1 + fake_policy=1 so zero-frame recordings actually land on
// disk via RecorderService. Run via the standard `make dashboard-e2e`.

import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

interface RobotEntry {
  id: string;
  name: string;
}

interface RecordingEntry {
  id: string;
}

async function deleteAllRobotsAndCameras(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

async function stopAllRecordings(request: APIRequestContext): Promise<void> {
  const recordings: RecordingEntry[] = await (await request.get("/api/recordings")).json();
  for (const r of recordings ?? []) {
    // best-effort: ignore failures (terminal sessions can't be stopped again)
    await request.post(`/api/recordings/${r.id}/stop`, { data: { save: false } });
  }
}

async function seedConnectedRobot(
  request: APIRequestContext,
  name: string,
): Promise<{ robotId: string }> {
  const cameraRes = await request.post("/api/cameras", {
    data: {
      name: `${name} cam`,
      backend: "opencv",
      source: { path: "/dev/fake-cam" },
      width: 320,
      height: 240,
      fps: 15,
    },
  });
  expect(cameraRes.ok(), `POST /api/cameras: ${cameraRes.status()}`).toBe(true);
  const camera = await cameraRes.json();
  const robotRes = await request.post("/api/robots", {
    data: {
      name,
      robot_type: "so101_follower",
      connection: { kind: "serial", port: "/dev/fake-acm" },
      cameras: [camera.id],
    },
  });
  expect(robotRes.ok(), `POST /api/robots: ${robotRes.status()}`).toBe(true);
  const robot: RobotEntry = await robotRes.json();
  const connectRes = await request.post(`/api/robots/${robot.id}/connect`);
  expect(connectRes.ok(), `connect: ${connectRes.status()} ${await connectRes.text()}`).toBe(true);
  return { robotId: robot.id };
}

test.describe("Recording", () => {
  test.beforeEach(async ({ request }) => {
    await stopAllRecordings(request);
    await deleteAllRobotsAndCameras(request);
  });

  test("start → progress indicator → stop+save shows summary", async ({
    page,
    request,
    captureFullPage,
  }) => {
    const { robotId } = await seedConnectedRobot(request, "Rec Bot");
    await page.goto(`/#/robots/${robotId}`);

    // Record button activates only when the robot reports online.
    const recordButton = page.getByRole("button", { name: /^Record$/ });
    await expect(recordButton).toBeEnabled({ timeout: 15_000 });
    await recordButton.click();

    const startDialog = page.getByRole("dialog");
    await expect(startDialog).toBeVisible();
    await expect(startDialog.getByText(/Record dataset/i)).toBeVisible();

    // Dataset name pre-fills with a timestamped slug; override it so the
    // assertion later is deterministic regardless of clock.
    await startDialog.getByLabel("Dataset name").fill("qa-rec-set");
    await startDialog.getByLabel("Task description").fill("QA recording smoke");
    await startDialog.getByLabel("Capture FPS").selectOption("15");
    await startDialog.getByRole("button", { name: /Start recording/i }).click();
    await expect(startDialog).toBeHidden({ timeout: 10_000 });

    const indicator = page.getByTestId("recording-indicator");
    await expect(indicator).toBeVisible({ timeout: 10_000 });
    await captureFullPage(page, "06_recording_active");

    // Let several progress heartbeats land so the dataset has frames to
    // finalize. The fake frame source emits at ~30 fps.
    await page.waitForTimeout(1_500);

    await indicator.getByRole("button", { name: /^Stop$/ }).click();
    const stopDialog = page.getByRole("dialog").filter({ hasText: /Stop recording/i });
    await expect(stopDialog).toBeVisible();
    await stopDialog.getByRole("button", { name: /Save dataset/i }).click();

    const summary = page.getByTestId("recording-summary");
    await expect(summary).toBeVisible({ timeout: 15_000 });
    await expect(summary).toContainText(/Recording saved/i);
    await expect(summary).toContainText(/qa-rec-set/);
    await captureFullPage(page, "06_recording_summary");

    // Server-side sanity: GET /api/recordings has the saved entry.
    const list = await (await request.get("/api/recordings")).json();
    const saved = (list ?? []).find(
      (r: { dataset_name: string; saved: boolean }) =>
        r.dataset_name === "qa-rec-set" && r.saved === true,
    );
    expect(saved, `qa-rec-set not saved in /api/recordings: ${JSON.stringify(list)}`).toBeTruthy();

    // Cleanup
    await request.post(`/api/robots/${robotId}/disconnect`);
  });

  test("Discard path leaves saved=false in /api/recordings", async ({ page, request }) => {
    const { robotId } = await seedConnectedRobot(request, "Discard Bot");
    await page.goto(`/#/robots/${robotId}`);
    const recordButton = page.getByRole("button", { name: /^Record$/ });
    await expect(recordButton).toBeEnabled({ timeout: 15_000 });
    await recordButton.click();

    const startDialog = page.getByRole("dialog");
    await startDialog.getByLabel("Dataset name").fill("qa-rec-discard");
    await startDialog.getByLabel("Task description").fill("Discard path");
    await startDialog.getByLabel("Capture FPS").selectOption("15");
    await startDialog.getByRole("button", { name: /Start recording/i }).click();
    await expect(startDialog).toBeHidden({ timeout: 10_000 });

    const indicator = page.getByTestId("recording-indicator");
    await expect(indicator).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(750);

    await indicator.getByRole("button", { name: /^Stop$/ }).click();
    const stopDialog = page.getByRole("dialog").filter({ hasText: /Stop recording/i });
    await stopDialog.getByRole("button", { name: /Discard/i }).click();

    const summary = page.getByTestId("recording-summary");
    await expect(summary).toBeVisible({ timeout: 15_000 });

    const list = await (await request.get("/api/recordings")).json();
    const discarded = (list ?? []).find(
      (r: { dataset_name: string; saved: boolean }) => r.dataset_name === "qa-rec-discard",
    );
    expect(discarded, "discard session missing").toBeTruthy();
    expect(discarded.saved).toBe(false);

    await request.post(`/api/robots/${robotId}/disconnect`);
  });
});
