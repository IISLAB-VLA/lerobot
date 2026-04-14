import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

interface RobotCreatePayload {
  name: string;
  robot_type: string;
  connection: { kind: "serial"; port: string };
  cameras: string[];
}

async function resetRegistry(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) await request.delete(`/api/robots/${r.id}`);
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) await request.delete(`/api/cameras/${c.id}`);
}

async function seedRobot(
  request: APIRequestContext,
  name: string,
  cameraCount: number,
): Promise<{ robotId: string }> {
  const cameraIds: string[] = [];
  for (let i = 0; i < cameraCount; i += 1) {
    const res = await request.post("/api/cameras", {
      data: {
        name: `${name} cam ${i}`,
        backend: "opencv",
        source: { path: `/dev/video${i}` },
        width: 640,
        height: 480,
        fps: 30,
      },
    });
    expect(res.ok(), `POST /api/cameras: ${res.status()} ${await res.text()}`).toBe(true);
    cameraIds.push((await res.json()).id);
  }
  const payload: RobotCreatePayload = {
    name,
    robot_type: "so101_follower",
    connection: { kind: "serial", port: "/dev/ttyFAKE0" },
    cameras: cameraIds,
  };
  const robotRes = await request.post("/api/robots", { data: payload });
  expect(robotRes.ok(), `POST /api/robots: ${robotRes.status()} ${await robotRes.text()}`).toBe(true);
  return { robotId: (await robotRes.json()).id };
}

// The frontend is served under a HashRouter, so deep-link via the hash.
async function gotoDetail(page: Page, robotId: string): Promise<void> {
  await page.goto(`/#/robots/${robotId}`);
  await expect(page).toHaveURL(new RegExp(`#/robots/${robotId}$`));
}

test.describe("Robot detail", () => {
  test.beforeEach(async ({ request }) => {
    await resetRegistry(request);
  });

  test("card click on Robots page navigates to detail", async ({ page, request }) => {
    const { robotId } = await seedRobot(request, "Nav Bot", 1);
    await page.goto("/");
    await page.getByRole("link", { name: /^Robots$/i }).click();
    await expect(page).toHaveURL(/#\/robots$/);
    await page.getByRole("button", { name: /Open Nav Bot/i }).click();
    await expect(page).toHaveURL(new RegExp(`#/robots/${robotId}$`));
    await expect(page.getByRole("heading", { level: 1, name: "Nav Bot" })).toBeVisible();
  });

  test("robot with cameras shows header, Pause button, and Grid+Spotlight enabled", async ({
    page,
    request,
    captureFullPage,
  }) => {
    const { robotId } = await seedRobot(request, "Detail Bot Cams", 2);
    await gotoDetail(page, robotId);

    await expect(page.getByRole("heading", { level: 1, name: "Detail Bot Cams" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Pause streams/i })).toBeVisible();

    const switcher = page.getByRole("toolbar", { name: /Stream layout/i });
    await expect(switcher).toBeVisible();
    await expect(switcher.getByRole("button", { name: "Single" })).toBeEnabled();
    await expect(switcher.getByRole("button", { name: "Grid" })).toBeEnabled();
    await expect(switcher.getByRole("button", { name: "Spotlight" })).toBeEnabled();

    await captureFullPage(page, "03_robot_detail_cams");
  });

  test("Pause/Resume toggle flips label", async ({ page, request }) => {
    const { robotId } = await seedRobot(request, "Pause Bot", 1);
    await gotoDetail(page, robotId);

    const toggle = page.getByRole("button", { name: /Pause streams|Resume streams/ });
    await expect(toggle).toHaveText(/Pause streams/);
    await toggle.click();
    await expect(toggle).toHaveText(/Resume streams/);
    await toggle.click();
    await expect(toggle).toHaveText(/Pause streams/);
  });

  test("number keys switch layout when tile count allows", async ({ page, request }) => {
    const { robotId } = await seedRobot(request, "Keybind Bot", 2);
    await gotoDetail(page, robotId);

    const switcher = page.getByRole("toolbar", { name: /Stream layout/i });
    const single = switcher.getByRole("button", { name: "Single" });
    const grid = switcher.getByRole("button", { name: "Grid" });
    const spotlight = switcher.getByRole("button", { name: "Spotlight" });

    // Initial: Single is selected.
    await expect(single).toHaveAttribute("aria-pressed", "true");

    await page.keyboard.press("4");
    await expect(grid).toHaveAttribute("aria-pressed", "true");
    await expect(single).toHaveAttribute("aria-pressed", "false");

    await page.keyboard.press("5");
    await expect(spotlight).toHaveAttribute("aria-pressed", "true");
    await expect(grid).toHaveAttribute("aria-pressed", "false");

    await page.keyboard.press("1");
    await expect(single).toHaveAttribute("aria-pressed", "true");
  });

  test("number key for a layout exceeding tile count is a no-op", async ({ page, request }) => {
    const { robotId } = await seedRobot(request, "Solo Cam Bot", 1);
    await gotoDetail(page, robotId);

    const switcher = page.getByRole("toolbar", { name: /Stream layout/i });
    const single = switcher.getByRole("button", { name: "Single" });
    const grid = switcher.getByRole("button", { name: "Grid" });

    await expect(single).toHaveAttribute("aria-pressed", "true");
    await expect(grid).toBeDisabled();

    await page.keyboard.press("4");
    // Grid requires 2 tiles; stays on Single.
    await expect(single).toHaveAttribute("aria-pressed", "true");
    await expect(grid).toHaveAttribute("aria-pressed", "false");
  });

  test("robot with no cameras shows empty state and multi-tile layouts disabled", async ({
    page,
    request,
    captureFullPage,
  }) => {
    const { robotId } = await seedRobot(request, "No Cam Bot", 0);
    await gotoDetail(page, robotId);

    await expect(page.getByText(/No cameras attached to this robot/i)).toBeVisible();

    const switcher = page.getByRole("toolbar", { name: /Stream layout/i });
    await expect(switcher.getByRole("button", { name: "Single" })).toBeDisabled();
    await expect(switcher.getByRole("button", { name: "Grid" })).toBeDisabled();
    await expect(switcher.getByRole("button", { name: "Spotlight" })).toBeDisabled();

    await captureFullPage(page, "03_robot_detail_no_cams");
  });
});
