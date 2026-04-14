// Hardware smoke — opt-in via `make dashboard-e2e-hardware`
// (sets LEROBOT_DASHBOARD_E2E_HARDWARE=1 so playwright.config flips the
// server into fake_devices=0 and selects the `hardware` project).
//
// Requires: SO-101 arm at /dev/ttyACM0, three USB cameras at /dev/video0,
// /dev/video2, /dev/video4. See tests/dashboard/e2e/README.md for setup.
//
// Scope: prove the production server can talk to real hardware end to end —
// device discovery → /api/robots + /api/cameras seeding → /connect →
// /robots/:id detail page → first WebRTC frame. The Add-Robot UI flow is
// already covered by spec 02 against fake devices and pulling it in here
// fights the v4l2 enumeration race that kicks in once a node has been opened.

import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

const EXPECTED_RAW_SERIAL = "/dev/ttyACM0";
const EXPECTED_CAMERA_PATH = "/dev/video0";

interface SerialPortEntry {
  port: string; // stable path (e.g. /dev/serial/by-id/...)
  raw_port: string; // raw device node (e.g. /dev/ttyACM0)
}

interface CameraDeviceEntry {
  id: string;
  path?: string | null;
  backend: string;
  default_profile?: {
    width?: number | null;
    height?: number | null;
    fps?: number | null;
  } | null;
}

async function resetRegistry(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

test.describe("Hardware smoke", { tag: "@hardware" }, () => {
  test.beforeEach(async ({ request }) => {
    await resetRegistry(request);
  });

  test("device discovery reports the expected serial port and video devices", async ({
    request,
  }) => {
    const serial = await (await request.get("/api/devices/serial")).json();
    const rawPorts: string[] = (serial?.ports ?? []).map((p: SerialPortEntry) => p.raw_port);
    expect(
      rawPorts,
      `expected raw_port=${EXPECTED_RAW_SERIAL} in /api/devices/serial, got ${JSON.stringify(rawPorts)}`,
    ).toContain(EXPECTED_RAW_SERIAL);

    const cams = await (await request.get("/api/devices/cameras")).json();
    const camPaths: string[] = (cams?.cameras ?? [])
      .map((c: CameraDeviceEntry) => c.path)
      .filter((p): p is string => !!p);
    expect(
      camPaths,
      `expected ${EXPECTED_CAMERA_PATH} in cameras, got ${JSON.stringify(camPaths)}`,
    ).toContain(EXPECTED_CAMERA_PATH);
  });

  test("seed SO-101 + belly cam via API and observe first WebRTC frame", async ({
    page,
    request,
  }) => {
    // 1. Resolve the discovery payloads once. Doing this back-to-back hits
    //    the v4l2 race the README warns about, but here we only need a single
    //    snapshot to seed from.
    const serial = await (await request.get("/api/devices/serial")).json();
    const serialEntry = (serial?.ports ?? []).find(
      (p: SerialPortEntry) => p.raw_port === EXPECTED_RAW_SERIAL,
    );
    expect(serialEntry, `no serial entry with raw_port=${EXPECTED_RAW_SERIAL}`).toBeTruthy();

    const cams = await (await request.get("/api/devices/cameras")).json();
    const cameraEntry: CameraDeviceEntry | undefined = (cams?.cameras ?? []).find(
      (c: CameraDeviceEntry) => c.path === EXPECTED_CAMERA_PATH,
    );
    expect(cameraEntry, `no camera entry with path=${EXPECTED_CAMERA_PATH}`).toBeTruthy();

    // 2. Seed the camera + robot directly through the public API. The UI
    //    add-robot flow is verified in spec 02; here we want the WebRTC path,
    //    not another UX run.
    // Use whatever the device actually supports — the v4l2 driver rejects
    // arbitrary fps requests (e.g. /dev/video0 here only does 25 fps).
    const profile = cameraEntry!.default_profile ?? {};
    const cameraRes = await request.post("/api/cameras", {
      data: {
        name: "belly",
        backend: cameraEntry!.backend,
        source: { path: cameraEntry!.path },
        width: profile.width ?? 640,
        height: profile.height ?? 480,
        fps: profile.fps ?? 30,
      },
    });
    expect(
      cameraRes.ok(),
      `POST /api/cameras failed: ${cameraRes.status()} ${await cameraRes.text()}`,
    ).toBe(true);
    const camera = await cameraRes.json();

    const robotRes = await request.post("/api/robots", {
      data: {
        name: "HW Smoke Bot",
        robot_type: "so101_follower",
        connection: { kind: "serial", port: serialEntry.port },
        cameras: [camera.id],
      },
    });
    expect(
      robotRes.ok(),
      `POST /api/robots failed: ${robotRes.status()} ${await robotRes.text()}`,
    ).toBe(true);
    const robot = await robotRes.json();

    // 3. Bring the robot online so streams_enabled flips on.
    const connectRes = await request.post(`/api/robots/${robot.id}/connect`);
    expect(
      connectRes.ok(),
      `connect failed: ${connectRes.status()} ${await connectRes.text()}`,
    ).toBe(true);

    // 4. Spy on the offer request and deep-link to the detail page.
    const offerRequest = page.waitForRequest(
      (req) => req.url().endsWith("/api/streams/offer") && req.method() === "POST",
      { timeout: 20_000 },
    );
    await page.goto(`/#/robots/${robot.id}`);
    await offerRequest;

    // 5. Wait for the <video> element to expose a non-zero readyState — that
    //    confirms a frame actually reached the renderer.
    const video = page.locator("video").first();
    await expect(video).toBeVisible({ timeout: 20_000 });
    await expect
      .poll(
        async () => await video.evaluate((el) => (el as HTMLVideoElement).readyState),
        { timeout: 30_000, intervals: [500, 1_000, 2_000] },
      )
      .toBeGreaterThanOrEqual(2);

    // 6. Tear down via the API. We never call pkill — Playwright's
    //    webServer owns the backend lifecycle, and `pkill -f lerobot-dashboard`
    //    also matches teammate agent command lines.
    const disconnectRes = await request.post(`/api/robots/${robot.id}/disconnect`);
    expect(
      disconnectRes.ok(),
      `disconnect failed: ${disconnectRes.status()} ${await disconnectRes.text()}`,
    ).toBe(true);
  });
});
