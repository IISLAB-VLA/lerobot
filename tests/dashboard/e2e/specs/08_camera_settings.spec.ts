// Camera settings drawer E2E — exercises the gear-button popover on each
// VideoTile in the robot detail streaming grid.
//
// The popover fetches GET /api/cameras/{id}/capabilities which is mocked so
// the spec is independent of the real v4l2 probe.
//
// Testids used (frontend commit 322e8080 adds the three inner ones):
//   camera-settings-btn    — gear icon button on VideoTile (hover-revealed)
//   camera-settings-panel  — popover container
//   camera-settings-resolution — capabilities <dd> for resolutions
//   camera-settings-fps        — capabilities <dd> for fps options
//   camera-settings-codec      — capabilities <dd> for codecs

import type { APIRequestContext, Route } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

const CAPABILITIES = {
  resolutions: [
    { width: 640, height: 480 },
    { width: 1280, height: 720 },
  ],
  fps_options: [15, 30],
  codecs: ["h264", "vp8"],
  current: { width: 640, height: 480, fps: 30, codec: null },
  source: "v4l2",
};

async function deleteAllRobotsAndCameras(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

async function seedRobotWithCamera(
  request: APIRequestContext,
  name: string,
): Promise<{ robotId: string; cameraId: string }> {
  const camRes = await request.post("/api/cameras", {
    data: {
      name: `${name} cam`,
      backend: "opencv",
      source: { path: "/dev/fake-cam" },
      width: 640,
      height: 480,
      fps: 30,
    },
  });
  expect(camRes.ok(), `POST /api/cameras: ${camRes.status()}`).toBe(true);
  const camera = await camRes.json();

  const robotRes = await request.post("/api/robots", {
    data: {
      name,
      robot_type: "so101_follower",
      connection: { kind: "serial", port: "/dev/fake-acm" },
      cameras: [camera.id],
    },
  });
  expect(robotRes.ok(), `POST /api/robots: ${robotRes.status()}`).toBe(true);
  const robot = await robotRes.json();
  await request.post(`/api/robots/${robot.id}/connect`);

  return { robotId: robot.id, cameraId: camera.id };
}

test.describe("Camera settings drawer", () => {
  test.beforeEach(async ({ request }) => {
    await deleteAllRobotsAndCameras(request);
  });

  test("gear button opens settings panel with capabilities", async ({ page, request }) => {
    const { robotId, cameraId } = await seedRobotWithCamera(request, "Cam Bot");

    // Mock capabilities endpoint.
    await page.route(`**/api/cameras/${cameraId}/capabilities`, (route: Route) =>
      route.fulfill({
        status: 200,
        headers: { "content-type": "application/json" },
        body: JSON.stringify(CAPABILITIES),
      }),
    );

    await page.goto(`/#/robots/${robotId}`);

    // Wait for the video tile to render.
    const tile = page.getByTestId("video-tile").first();
    await expect(tile).toBeVisible({ timeout: 15_000 });

    // The gear button is hidden until hover — reveal it.
    await tile.hover();
    const gearBtn = tile.getByTestId("camera-settings-btn");
    await expect(gearBtn).toBeVisible({ timeout: 3_000 });
    await gearBtn.click();

    // Panel should appear.
    const panel = page.getByTestId("camera-settings-panel");
    await expect(panel).toBeVisible({ timeout: 5_000 });

    // Capabilities load (React Query fetches mock). Verify content.
    // Source label: "v4l2" → "V4L2 (local USB/CSI)"
    await expect(panel).toContainText(/V4L2/i, { timeout: 5_000 });

    // Active mode: "640×480 @ 30 fps"
    await expect(panel).toContainText(/640.480/);
    await expect(panel).toContainText(/30 fps/);

    // Resolutions list — testid added in 322e8080.
    const resList = panel.getByTestId("camera-settings-resolution");
    await expect(resList).toContainText("640×480");
    await expect(resList).toContainText("1280×720");

    // FPS options.
    const fpsList = panel.getByTestId("camera-settings-fps");
    await expect(fpsList).toContainText("15");
    await expect(fpsList).toContainText("30");

    // Codec badges — first (h264) is preferred (primary styling).
    const codecEl = panel.getByTestId("camera-settings-codec");
    await expect(codecEl).toContainText("h264");
    await expect(codecEl).toContainText("vp8");
  });

  test("closing the panel with X button hides it", async ({ page, request }) => {
    const { robotId, cameraId } = await seedRobotWithCamera(request, "Cam Bot 2");

    await page.route(`**/api/cameras/${cameraId}/capabilities`, (route: Route) =>
      route.fulfill({
        status: 200,
        headers: { "content-type": "application/json" },
        body: JSON.stringify(CAPABILITIES),
      }),
    );

    await page.goto(`/#/robots/${robotId}`);
    const tile = page.getByTestId("video-tile").first();
    await expect(tile).toBeVisible({ timeout: 15_000 });
    await tile.hover();
    await tile.getByTestId("camera-settings-btn").click();

    const panel = page.getByTestId("camera-settings-panel");
    await expect(panel).toBeVisible({ timeout: 5_000 });

    // Click the X close button.
    await panel.getByRole("button", { name: /close settings/i }).click();
    await expect(panel).toBeHidden({ timeout: 3_000 });
  });

  test("gear toggle closes panel on second click", async ({ page, request }) => {
    const { robotId, cameraId } = await seedRobotWithCamera(request, "Cam Bot 3");

    await page.route(`**/api/cameras/${cameraId}/capabilities`, (route: Route) =>
      route.fulfill({
        status: 200,
        headers: { "content-type": "application/json" },
        body: JSON.stringify(CAPABILITIES),
      }),
    );

    await page.goto(`/#/robots/${robotId}`);
    const tile = page.getByTestId("video-tile").first();
    await expect(tile).toBeVisible({ timeout: 15_000 });
    await tile.hover();
    const gearBtn = tile.getByTestId("camera-settings-btn");

    // Open.
    await gearBtn.click();
    await expect(page.getByTestId("camera-settings-panel")).toBeVisible({ timeout: 5_000 });

    // Close via toggle (second click).
    await gearBtn.click();
    await expect(page.getByTestId("camera-settings-panel")).toBeHidden({ timeout: 3_000 });
  });

  test("error state shown when capabilities fetch fails", async ({ page, request }) => {
    const { robotId, cameraId } = await seedRobotWithCamera(request, "Cam Bot 4");

    await page.route(`**/api/cameras/${cameraId}/capabilities`, (route: Route) =>
      route.fulfill({ status: 500, body: "{}" }),
    );

    await page.goto(`/#/robots/${robotId}`);
    const tile = page.getByTestId("video-tile").first();
    await expect(tile).toBeVisible({ timeout: 15_000 });
    await tile.hover();
    await tile.getByTestId("camera-settings-btn").click();

    const panel = page.getByTestId("camera-settings-panel");
    await expect(panel).toBeVisible({ timeout: 5_000 });
    await expect(panel.getByRole("alert")).toContainText(/failed/i, { timeout: 5_000 });
  });
});
