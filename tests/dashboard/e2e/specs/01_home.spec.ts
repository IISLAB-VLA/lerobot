import { test, expect } from "../fixtures/dashboard";

test.describe("Home route", () => {
  test("renders dashboard heading and backend health card", async ({
    page,
    captureFullPage,
  }) => {
    await page.goto("/");

    await expect(
      page.getByRole("heading", { level: 1, name: /LeRobot Dashboard/i }),
    ).toBeVisible();

    const healthCard = page.getByText(/Backend health/i);
    await expect(healthCard).toBeVisible();

    const apiPath = page.getByText("/api/health", { exact: true });
    await expect(apiPath).toBeVisible();

    await expect(page.getByText(/^ok$/i)).toBeVisible({ timeout: 10_000 });

    await captureFullPage(page, "01_home");
  });

  test("home page stays on /api/health under polling (no navigation)", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/$/);
    await page.waitForTimeout(1_000);
    await expect(page).toHaveURL(/\/$/);
  });
});
