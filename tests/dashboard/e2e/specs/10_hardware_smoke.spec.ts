// Hardware smoke — opt-in via `make dashboard-e2e-hardware`
// (sets LEROBOT_DASHBOARD_E2E_HARDWARE=1 so playwright.config flips the
// server into fake_devices=0 and selects the `hardware` project).
//
// Requires: SO-101 arm at /dev/ttyACM0, three USB cameras at /dev/video0,
// /dev/video2, /dev/video4. Mirror the setup in tests/dashboard/e2e/README.md.
//
// This is a minimal path-smoke: device discovery → UI add-robot → detail
// page → first WebRTC frame. It does NOT assert frame content or fps.

import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

const EXPECTED_SERIAL = "/dev/ttyACM0";
const EXPECTED_CAMERA = "/dev/video0"; // belly cam

async function resetRegistry(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

async function fillSerialPort(
  scope: import("@playwright/test").Locator,
  value: string,
): Promise<void> {
  const port = scope.getByLabel("Serial port");
  await expect(port).toBeVisible();
  const tag = await port.evaluate((el) => (el as HTMLElement).tagName.toLowerCase());
  if (tag === "select") {
    await port.selectOption(value);
  } else {
    await port.fill(value);
  }
}

test.describe("Hardware smoke", { tag: "@hardware" }, () => {
  test.beforeEach(async ({ request }) => {
    await resetRegistry(request);
  });

  test("device discovery reports the expected serial port and video devices", async ({
    request,
  }) => {
    const serial = await (await request.get("/api/devices/serial")).json();
    const serialPorts: string[] = (serial?.ports ?? []).map((p: { port: string }) => p.port);
    expect(
      serialPorts,
      `expected ${EXPECTED_SERIAL} in /api/devices/serial, got ${JSON.stringify(serialPorts)}`,
    ).toContain(EXPECTED_SERIAL);

    const cams = await (await request.get("/api/devices/cameras")).json();
    const camPaths: string[] = (cams?.cameras ?? [])
      .map((c: { path?: string | null }) => c.path)
      .filter((p: string | null | undefined): p is string => !!p);
    expect(camPaths, `expected /dev/video0 in cameras, got ${JSON.stringify(camPaths)}`).toContain(
      EXPECTED_CAMERA,
    );
  });

  test("add SO-101 + belly cam via UI and observe first WebRTC frame", async ({
    page,
    request,
  }) => {
    // 1. Register through the UI so we exercise the same flow the operator
    //    actually uses (Add Robot modal → Step 1 → Step 2 serial → Step 3 cam).
    await page.goto("/#/robots");
    await page.getByRole("button", { name: /^Add robot$/ }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name").fill("HW Smoke Bot");
    await dialog.getByLabel("Robot type").selectOption("so101_follower");
    await dialog.getByRole("button", { name: /^Next$/ }).click();
    await fillSerialPort(dialog, EXPECTED_SERIAL);
    await dialog.getByRole("button", { name: /^Next$/ }).click();
    await dialog.getByRole("button", { name: /Add camera/i }).click();
    const cameraRow = dialog.getByRole("listitem", { name: /Camera 1/i });
    await cameraRow.getByLabel("Camera name").fill("belly");
    const deviceControl = cameraRow.locator("select").last();
    if (await deviceControl.count()) {
      await deviceControl.selectOption(EXPECTED_CAMERA);
    } else {
      await cameraRow.getByPlaceholder("/dev/video0").fill(EXPECTED_CAMERA);
    }
    await dialog.getByRole("button", { name: /Create robot/i }).click();
    await expect(dialog).toBeHidden({ timeout: 15_000 });

    // 2. Look the robot up so we can deep-link to its detail page.
    const robots: { id: string; name: string }[] = await (await request.get("/api/robots")).json();
    const robot = robots.find((r) => r.name === "HW Smoke Bot");
    expect(robot, "robot was not created").toBeTruthy();

    // 3. Bring the robot online — the manager endpoint is the same one the
    //    detail page hints at. Without this the streams stay disabled.
    const connectRes = await request.post(`/api/robots/${robot!.id}/connect`);
    expect(
      connectRes.ok(),
      `connect failed: ${connectRes.status()} ${await connectRes.text()}`,
    ).toBe(true);

    // 4. Hop to detail view and spy on the offer request.
    const offerRequest = page.waitForRequest(
      (req) => req.url().endsWith("/api/streams/offer") && req.method() === "POST",
      { timeout: 20_000 },
    );
    await page.goto(`/#/robots/${robot!.id}`);
    await offerRequest;

    // 5. Wait for the <video> element to expose a non-zero readyState,
    //    which signals the first frame reached the renderer.
    const video = page.locator("video").first();
    await expect(video).toBeVisible({ timeout: 20_000 });
    await expect
      .poll(
        async () => await video.evaluate((el) => (el as HTMLVideoElement).readyState),
        { timeout: 30_000, intervals: [500, 1_000, 2_000] },
      )
      .toBeGreaterThanOrEqual(2);

    // 6. Disconnect cleanly so the next manual run starts from a clean state.
    //    We don't pkill the server — Playwright's webServer owns its lifecycle.
    const disconnectRes = await request.post(`/api/robots/${robot!.id}/disconnect`);
    expect(
      disconnectRes.ok(),
      `disconnect failed: ${disconnectRes.status()} ${await disconnectRes.text()}`,
    ).toBe(true);
  });
});
