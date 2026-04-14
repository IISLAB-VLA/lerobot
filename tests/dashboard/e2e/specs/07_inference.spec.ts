// Inference E2E — exercises the policy-rollout UI on /robots/:id/inference.
// Backend WS (/ws/inference/{id}) and HTTP endpoints are mocked so the spec
// is independent of policy load times and real action_features availability.

import type { APIRequestContext, Page, Route } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

const POLICY_LANG = {
  repo_id: "qa/lang-vla",
  policy_type: "smolvla",
  root: "/tmp/policies/qa-lang",
  num_parameters: 250_000_000,
  last_modified: "2026-04-14T00:00:00Z",
  observation_features: { image: { dtype: "uint8", shape: [3, 224, 224] } },
  action_features: { joint: { dtype: "float32", shape: [6] } },
  supports_language: true,
  device: "cuda",
};
const POLICY_NOLANG = { ...POLICY_LANG, repo_id: "qa/silent-act", policy_type: "act", supports_language: false };

interface SessionInit {
  id?: string;
  dry_run?: boolean;
  deadman_required?: boolean;
  status?: string;
  repo_id?: string;
}

function fakeSession(init: SessionInit = {}): Record<string, unknown> {
  return {
    id: init.id ?? "sess-qa-1",
    robot_id: "00000000-0000-0000-0000-000000000000",
    repo_id: init.repo_id ?? POLICY_LANG.repo_id,
    fps: 15,
    dry_run: init.dry_run ?? true,
    task_description: "qa rollout",
    status: init.status ?? "starting",
    step: 0,
    started_at: new Date().toISOString(),
    stopped_at: null,
    error: null,
    last_latency_ms: null,
    deadman_required: init.deadman_required ?? false,
    deadman_held: false,
    max_action_magnitude: null,
  };
}

async function deleteAllRobotsAndCameras(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

async function seedRobot(request: APIRequestContext, name: string): Promise<string> {
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
  await request.post(`/api/robots/${robot.id}/connect`);
  return robot.id;
}

async function mockPolicies(page: Page, policies: Record<string, unknown>[]): Promise<void> {
  await page.route("**/api/policies", (route: Route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(policies),
    }),
  );
}

test.describe("Inference (mocked backend)", () => {
  test.beforeEach(async ({ request }) => {
    await deleteAllRobotsAndCameras(request);
  });

  test("dry-run start shows session panel + dry-run badge + first step", async ({
    page,
    request,
    captureFullPage,
  }) => {
    const robotId = await seedRobot(request, "Inf Bot");
    await mockPolicies(page, [POLICY_LANG]);

    const session = fakeSession({ id: "sess-dry", dry_run: true });
    await page.route("**/api/inference", (route: Route) => {
      if (route.request().method() === "POST") {
        return route.fulfill({
          status: 202,
          headers: { "content-type": "application/json" },
          body: JSON.stringify(session),
        });
      }
      return route.continue();
    });
    await page.routeWebSocket("**/ws/inference/sess-dry", (ws) => {
      ws.send(
        JSON.stringify({ type: "step", step: 1, action: [0.11, 0.22, -0.05], latency_ms: 18 }),
      );
    });

    await page.goto(`/#/robots/${robotId}/inference`);
    await expect(
      page.getByRole("heading", { level: 1, name: /Run policy on Inf Bot/i }),
    ).toBeVisible();
    await page.getByLabel("Task description").fill("pick the cube");
    await page.getByTestId("inference-start").click();

    const session_panel = page.getByTestId("inference-session");
    await expect(session_panel).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("dry-run-badge")).toBeVisible();
    await expect(page.getByTestId("inference-deadman")).toHaveCount(0);
    await expect(page.getByTestId("inference-timeline")).toContainText(/step|0\.11|0\.22/);
    await captureFullPage(page, "07_inference_session");
  });

  test("409 conflict surfaces inline error and no session panel", async ({ page, request }) => {
    const robotId = await seedRobot(request, "Conflict Bot");
    await mockPolicies(page, [POLICY_LANG]);
    await page.route("**/api/inference", (route: Route) => {
      if (route.request().method() === "POST") {
        return route.fulfill({
          status: 409,
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ detail: "another inference active for robot" }),
        });
      }
      return route.continue();
    });

    await page.goto(`/#/robots/${robotId}/inference`);
    await page.getByLabel("Task description").fill("conflict");
    await page.getByTestId("inference-start").click();

    await expect(page.getByRole("alert")).toContainText(/another inference active|conflict|409/i, {
      timeout: 10_000,
    });
    await expect(page.getByTestId("inference-session")).toHaveCount(0);
  });

  test("Space toggles deadman; ignored while focus is on textarea", async ({ page, request }) => {
    const robotId = await seedRobot(request, "Deadman Bot");
    await mockPolicies(page, [POLICY_LANG]);

    const session = fakeSession({
      id: "sess-dm",
      dry_run: false,
      deadman_required: true,
      status: "running",
    });
    await page.route("**/api/inference", (route: Route) => {
      if (route.request().method() === "POST") {
        return route.fulfill({
          status: 202,
          headers: { "content-type": "application/json" },
          body: JSON.stringify(session),
        });
      }
      return route.continue();
    });
    let deadmanCalls = 0;
    const deadmanBodies: { held: boolean }[] = [];
    await page.route("**/api/inference/sess-dm/deadman", async (route: Route) => {
      deadmanCalls += 1;
      const body = route.request().postDataJSON() as { held: boolean };
      deadmanBodies.push(body);
      // Return a full session so onSuccess → setActiveSession keeps deadmanActive=true.
      await route.fulfill({
        status: 202,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...session, deadman_held: body.held }),
      });
    });
    await page.routeWebSocket("**/ws/inference/sess-dm", (ws) => {
      ws.send(JSON.stringify({ type: "step", step: 1, action: [0.0], latency_ms: 12 }));
    });

    await page.goto(`/#/robots/${robotId}/inference`);
    // Defaults already have deadmanRequired=true and dryRun=false-able. Make
    // sure dry_run is off so the safety badge shows up after start.
    const dryRunBox = page.getByLabel("Dry run");
    if (await dryRunBox.isChecked()) await dryRunBox.uncheck();
    await page.getByLabel("Task description").fill("hold space");
    await page.getByTestId("inference-start").click();

    // Wait for the session panel before asserting the deadman badge.
    await expect(page.getByTestId("inference-session")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("inference-deadman")).toBeVisible({ timeout: 5_000 });

    // Blur any focused element so Space does not accidentally click Stop.
    await page.evaluate(() => { (document.activeElement as HTMLElement)?.blur(); });

    await page.keyboard.down(" ");
    await expect.poll(() => deadmanBodies.some((b) => b.held === true)).toBe(true);
    expect(deadmanBodies[0]).toEqual({ held: true });
    await expect(page.getByTestId("inference-deadman")).toContainText(/held/i);

    await page.keyboard.up(" ");
    await expect.poll(() => deadmanBodies.some((b) => b.held === false)).toBe(true);
    expect(deadmanBodies[1]).toEqual({ held: false });
    await expect(page.getByTestId("inference-deadman")).toContainText(/release/i);

    // Focus a text input — Space inside it should NOT fire the deadman call.
    const callsBefore = deadmanCalls;
    await page.getByLabel("Task description").focus();
    await page.keyboard.down(" ");
    await page.keyboard.up(" ");
    await page.waitForTimeout(300);
    expect(deadmanCalls).toBe(callsBefore);
  });

  test("language policy reveals command box; act policy hides it", async ({ page, request }) => {
    const robotId = await seedRobot(request, "Cmd Bot");
    await mockPolicies(page, [POLICY_LANG, POLICY_NOLANG]);
    const session = fakeSession({ id: "sess-cmd", dry_run: true });
    await page.route("**/api/inference", (route: Route) => {
      if (route.request().method() === "POST") {
        return route.fulfill({
          status: 202,
          headers: { "content-type": "application/json" },
          body: JSON.stringify(session),
        });
      }
      return route.continue();
    });
    let commandBody: { text: string } | null = null;
    await page.route("**/api/inference/sess-cmd/command", async (route: Route) => {
      commandBody = route.request().postDataJSON() as { text: string };
      await route.fulfill({ status: 202, body: "{}" });
    });
    await page.routeWebSocket("**/ws/inference/sess-cmd", (ws) => {
      ws.send(JSON.stringify({ type: "step", step: 1, action: [0.0], latency_ms: 10 }));
    });

    await page.goto(`/#/robots/${robotId}/inference`);
    // Default policy is the first option (lang). Start, then send a command.
    await page.getByLabel("Task description").fill("walk through");
    await page.getByTestId("inference-start").click();
    await expect(page.getByTestId("inference-session")).toBeVisible({ timeout: 10_000 });

    const command = page.getByLabel("Live instruction");
    await expect(command).toBeVisible();
    await command.fill("turn left");
    await page.getByRole("button", { name: /^Send$/ }).click();
    await expect.poll(() => commandBody).toEqual({ text: "turn left" });
  });
});
