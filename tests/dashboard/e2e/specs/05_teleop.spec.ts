// Teleop E2E — exercises the TeleopPanel on /robots/:id.
// The backend WebSocket (/ws/robots/{id}/teleop) is mocked via routeWebSocket
// so specs run without a physical robot and without the real WS handler.
//
// Protocol constants (from lib/teleop/protocol.ts):
//   CLOSE_CONFLICT = 4001   → phase="conflict"
//   CLOSE_ROBOT_OFFLINE = 4002 → phase="offline"
//   HEARTBEAT_INTERVAL_MS = 100
//
// TeleopPanel testids:
//   teleop-panel, teleop-deadman (badge), teleop-deadman-toggle,
//   teleop-phase, teleop-error, teleop-telemetry

import type { APIRequestContext, WebSocketRoute } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

const WS_CONFLICT = 4001;
const WS_OFFLINE = 4002;

// State frame the real backend sends right after WS opens.
const STATE_IDLE = JSON.stringify({
  type: "state",
  payload: { state: "idle" },
  seq: 1,
  ts_server_ms: Date.now(),
});

const TELEMETRY_FRAME = JSON.stringify({
  type: "telemetry",
  payload: {
    deadman: "idle",
    forwarded: 12,
    dropped_deadman: 2,
    dropped_validation: 1,
    aux_events_forwarded: 3,
  },
  seq: 2,
  ts_server_ms: Date.now(),
});

async function deleteAllRobotsAndCameras(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

async function seedConnectedRobot(
  request: APIRequestContext,
  name: string,
): Promise<string> {
  const res = await request.post("/api/robots", {
    data: {
      name,
      robot_type: "so101_follower",
      connection: { kind: "serial", port: "/dev/fake-acm" },
      cameras: [],
    },
  });
  expect(res.ok(), `POST /api/robots: ${res.status()}`).toBe(true);
  const robot = await res.json();
  await request.post(`/api/robots/${robot.id}/connect`);
  return robot.id;
}

test.describe("TeleopPanel (mocked WS)", () => {
  test.beforeEach(async ({ request }) => {
    await deleteAllRobotsAndCameras(request);
  });

  test("panel appears when robot is online; deadman badge starts idle", async ({
    page,
    request,
  }) => {
    const robotId = await seedConnectedRobot(request, "Teleop Bot");

    // Mock: server sends idle state frame immediately on connect.
    await page.routeWebSocket(`**/ws/robots/${robotId}/teleop`, (ws: WebSocketRoute) => {
      ws.send(STATE_IDLE);
    });

    await page.goto(`/#/robots/${robotId}`);

    const panel = page.getByTestId("teleop-panel");
    await expect(panel).toBeVisible({ timeout: 10_000 });

    // Phase indicator shows "live" once the idle frame arrives (connected state).
    await expect(page.getByTestId("teleop-phase")).toContainText(/live|connected|idle/i, {
      timeout: 5_000,
    });

    // Deadman badge defaults to "idle" (no space held).
    await expect(page.getByTestId("teleop-deadman")).toContainText(/idle/i);

    // Toggle button is enabled (robot is online and connected).
    await expect(page.getByTestId("teleop-deadman-toggle")).toBeEnabled({ timeout: 5_000 });
  });

  test("Space hold/release updates deadman badge and sends WS frames", async ({
    page,
    request,
  }) => {
    const robotId = await seedConnectedRobot(request, "Deadman Bot");

    const sentFrames: unknown[] = [];
    await page.routeWebSocket(`**/ws/robots/${robotId}/teleop`, (ws: WebSocketRoute) => {
      ws.send(STATE_IDLE);
      // Capture client→server frames and ack non-heartbeat frames.
      // Only ONE onMessage handler is supported; combine both responsibilities here.
      ws.onMessage((raw) => {
        try {
          const frame = JSON.parse(raw as string) as { seq: number; type: string };
          sentFrames.push(frame);
          if (frame.type !== "heartbeat") {
            ws.send(
              JSON.stringify({
                type: "ack",
                payload: { seq: frame.seq },
                seq: 999,
                ts_server_ms: Date.now(),
              }),
            );
          }
        } catch {
          /* ignore */
        }
      });
    });

    await page.goto(`/#/robots/${robotId}`);
    await expect(page.getByTestId("teleop-deadman-toggle")).toBeEnabled({ timeout: 10_000 });

    // Blur focus so Space goes to window handler (not a button).
    await page.evaluate(() => { (document.activeElement as HTMLElement)?.blur(); });

    // Hold Space → deadman held.
    await page.keyboard.down(" ");
    await expect.poll(() => sentFrames.some((f: unknown) => {
      const frame = f as { type: string; payload: { held: boolean } };
      return frame.type === "deadman" && frame.payload.held === true;
    })).toBe(true);
    await expect(page.getByTestId("teleop-deadman-toggle")).toHaveAttribute("aria-pressed", "true", { timeout: 5_000 });

    // Release Space → deadman released.
    await page.keyboard.up(" ");
    await expect.poll(() => sentFrames.some((f: unknown) => {
      const frame = f as { type: string; payload: { held: boolean } };
      return frame.type === "deadman" && frame.payload.held === false;
    })).toBe(true);
    await expect(page.getByTestId("teleop-deadman-toggle")).toHaveAttribute("aria-pressed", "false", { timeout: 5_000 });
  });

  test("keyboard WASD sends input frames to WS", async ({ page, request }) => {
    const robotId = await seedConnectedRobot(request, "WASD Bot");

    const inputFrames: unknown[] = [];
    await page.routeWebSocket(`**/ws/robots/${robotId}/teleop`, (ws: WebSocketRoute) => {
      ws.send(STATE_IDLE);
      ws.onMessage((raw) => {
        try {
          const frame = JSON.parse(raw as string) as { type: string };
          if (frame.type === "input") inputFrames.push(frame);
          // Ack all non-heartbeat so the hook doesn't get confused.
          if (frame.type !== "heartbeat") {
            ws.send(JSON.stringify({ type: "ack", payload: { seq: (frame as { seq?: number }).seq ?? 0 }, seq: 999, ts_server_ms: Date.now() }));
          }
        } catch {
          /* ignore */
        }
      });
    });

    await page.goto(`/#/robots/${robotId}`);
    await expect(page.getByTestId("teleop-deadman-toggle")).toBeEnabled({ timeout: 10_000 });

    // Blur focus away from any button.
    await page.evaluate(() => { (document.activeElement as HTMLElement)?.blur(); });

    await page.keyboard.down("w");
    await expect.poll(() => inputFrames.some((f: unknown) => {
      const frame = f as { type: string; payload: { kind: string; key: string; pressed: boolean } };
      return frame.type === "input" && frame.payload.key === "w" && frame.payload.pressed === true;
    })).toBe(true);

    await page.keyboard.up("w");
    await expect.poll(() => inputFrames.some((f: unknown) => {
      const frame = f as { type: string; payload: { kind: string; key: string; pressed: boolean } };
      return frame.type === "input" && frame.payload.key === "w" && frame.payload.pressed === false;
    })).toBe(true);
  });

  test("conflict close (4001) shows conflict phase", async ({ page, request }) => {
    const robotId = await seedConnectedRobot(request, "Conflict Bot");

    await page.routeWebSocket(`**/ws/robots/${robotId}/teleop`, (ws: WebSocketRoute) => {
      // Immediately close with conflict code — robot already controlled by another client.
      ws.close({ code: WS_CONFLICT, reason: "another client connected" });
    });

    await page.goto(`/#/robots/${robotId}`);
    await expect(page.getByTestId("teleop-panel")).toBeVisible({ timeout: 10_000 });

    await expect(page.getByTestId("teleop-phase")).toContainText(/conflict/i, { timeout: 5_000 });
    await expect(page.getByTestId("teleop-error")).toContainText(/another client/i, {
      timeout: 5_000,
    });
    // Toggle disabled since we can't control while in conflict.
    await expect(page.getByTestId("teleop-deadman-toggle")).toBeDisabled();
  });

  test("offline close (4002) shows robot offline phase", async ({ page, request }) => {
    const robotId = await seedConnectedRobot(request, "Offline Bot");

    await page.routeWebSocket(`**/ws/robots/${robotId}/teleop`, (ws: WebSocketRoute) => {
      ws.close({ code: WS_OFFLINE, reason: "robot offline" });
    });

    await page.goto(`/#/robots/${robotId}`);
    await expect(page.getByTestId("teleop-panel")).toBeVisible({ timeout: 10_000 });

    await expect(page.getByTestId("teleop-phase")).toContainText(/offline/i, { timeout: 5_000 });
    await expect(page.getByTestId("teleop-deadman-toggle")).toBeDisabled();
  });

  test("telemetry frame populates forwarded/dropped counters", async ({ page, request }) => {
    const robotId = await seedConnectedRobot(request, "Telemetry Bot");

    await page.routeWebSocket(`**/ws/robots/${robotId}/teleop`, (ws: WebSocketRoute) => {
      ws.send(STATE_IDLE);
      // Push a telemetry frame after a short delay.
      setTimeout(() => {
        ws.send(TELEMETRY_FRAME);
      }, 300);
    });

    await page.goto(`/#/robots/${robotId}`);
    await expect(page.getByTestId("teleop-panel")).toBeVisible({ timeout: 10_000 });

    const telemetry = page.getByTestId("teleop-telemetry");
    await expect(telemetry).toBeVisible({ timeout: 5_000 });
    // forwarded=12, dropped_deadman+dropped_validation=3, aux=3.
    await expect(telemetry).toContainText("12");
    await expect(telemetry).toContainText("3");
  });

  test("lockout error is shown in teleop-error with reason", async ({ page, request }) => {
    const robotId = await seedConnectedRobot(request, "Lockout Bot");

    await page.routeWebSocket(`**/ws/robots/${robotId}/teleop`, (ws: WebSocketRoute) => {
      ws.send(STATE_IDLE);
      // Send lockout state after 300ms.
      setTimeout(() => {
        ws.send(
          JSON.stringify({
            type: "state",
            payload: { state: "lockout", reason: "heartbeat_timeout" },
            seq: 2,
            ts_server_ms: Date.now(),
          }),
        );
      }, 300);
    });

    await page.goto(`/#/robots/${robotId}`);
    await expect(page.getByTestId("teleop-panel")).toBeVisible({ timeout: 10_000 });

    await expect(page.getByTestId("teleop-error")).toContainText(/lockout.*heartbeat_timeout/i, {
      timeout: 5_000,
    });
    await expect(page.getByTestId("teleop-deadman")).toContainText(/lockout/i);
  });
});
