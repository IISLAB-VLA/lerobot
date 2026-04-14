import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect } from "../fixtures/dashboard";

async function resetRegistry(request: APIRequestContext): Promise<void> {
  const robots = await (await request.get("/api/robots")).json();
  for (const r of robots ?? []) {
    await request.delete(`/api/robots/${r.id}`);
  }
  const cameras = await (await request.get("/api/cameras")).json();
  for (const c of cameras ?? []) {
    await request.delete(`/api/cameras/${c.id}`);
  }
}

async function fillSerialPort(
  scope: import("@playwright/test").Locator,
  value: string,
): Promise<void> {
  const port = scope.getByLabel("Serial port");
  await expect(port).toBeVisible();
  const tag = await port.evaluate((el) => (el as HTMLElement).tagName.toLowerCase());
  if (tag === "select") {
    const options = await port.evaluate((el) =>
      Array.from((el as HTMLSelectElement).options)
        .map((o) => o.value)
        .filter((v) => v !== ""),
    );
    expect(options.length).toBeGreaterThan(0);
    await port.selectOption(options[0]);
  } else {
    await port.fill(value);
  }
}

async function gotoRobotsViaNav(page: Page): Promise<void> {
  // Backend serves index.html at /; deep-linking /robots 404s until SPA
  // fallback is added server-side. Use the nav link so React Router handles
  // routing client-side.
  await page.goto("/");
  await page.getByRole("link", { name: /^Robots$/i }).click();
  await expect(page).toHaveURL(/\/robots$/);
}

test.describe("Add Robot modal", () => {
  test.beforeEach(async ({ request }) => {
    await resetRegistry(request);
  });

  test("Robots page shows Add Robot button and empty state", async ({ page, captureFullPage }) => {
    await gotoRobotsViaNav(page);
    await expect(page.getByRole("heading", { level: 1, name: /^Robots$/ })).toBeVisible();
    await expect(page.getByTestId("robots-empty-state")).toBeVisible();
    await expect(page.getByRole("button", { name: /Add your first robot/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Add robot$/ })).toBeVisible();
    await captureFullPage(page, "02_robots_empty");
  });

  test("creates a serial robot without cameras and shows it in the grid", async ({
    page,
    captureFullPage,
  }) => {
    await gotoRobotsViaNav(page);
    await page.getByRole("button", { name: /^Add robot$/ }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(/Step 1 of 3/i)).toBeVisible();

    await dialog.getByLabel("Name").fill("QA Serial Bot");
    await dialog.getByLabel("Robot type").selectOption("so101_follower");
    await expect(dialog.getByRole("radio", { name: /Serial \/ USB/i })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await dialog.getByRole("button", { name: /^Next$/ }).click();

    await expect(dialog.getByText(/Step 2 of 3/i)).toBeVisible();
    // NOTE: /api/devices/serial returns {ports: [...]} but the frontend
    // currently types the response as a bare array, so the UI renders the
    // free-text fallback Input. Reported to frontend-architect. Once the
    // API layer matches the envelope, switch to .selectOption() here.
    await fillSerialPort(dialog, "/dev/ttyFAKE0");
    await dialog.getByRole("button", { name: /^Next$/ }).click();

    await expect(dialog.getByText(/Step 3 of 3/i)).toBeVisible();
    await expect(dialog.getByText(/No cameras yet/i)).toBeVisible();

    await captureFullPage(page, "02_add_robot_step3");

    await dialog.getByRole("button", { name: /Create robot/i }).click();
    await expect(dialog).toBeHidden({ timeout: 10_000 });

    await expect(page.getByText("QA Serial Bot").first()).toBeVisible({ timeout: 5_000 });
    await captureFullPage(page, "02_robots_after_create");
  });

  test("adds one camera and creates the robot", async ({ page }) => {
    await gotoRobotsViaNav(page);
    await page.getByRole("button", { name: /^Add robot$/ }).click();

    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name").fill("QA Bot With Cam");
    await dialog.getByLabel("Robot type").selectOption("so101_follower");
    await dialog.getByRole("button", { name: /^Next$/ }).click();

    await fillSerialPort(dialog, "/dev/ttyFAKE1");
    await dialog.getByRole("button", { name: /^Next$/ }).click();

    await expect(dialog.getByText(/Step 3 of 3/i)).toBeVisible();
    await dialog.getByRole("button", { name: /Add camera/i }).click();

    const cameraRow = dialog.getByRole("listitem", { name: /Camera 1/i });
    await expect(cameraRow).toBeVisible();
    await cameraRow.getByLabel("Camera name").fill("Front cam");
    // "Device" label has no htmlFor binding in the modal, so getByLabel misses.
    // Target the fallback Input by its placeholder. When the envelope bug is
    // fixed upstream, swap this for .selectOption() on the device dropdown.
    await cameraRow.getByPlaceholder("/dev/video0").fill("/dev/video1");

    await dialog.getByRole("button", { name: /Create robot/i }).click();
    await expect(dialog).toBeHidden({ timeout: 10_000 });

    await expect(page.getByText("QA Bot With Cam").first()).toBeVisible({ timeout: 5_000 });
  });
});
