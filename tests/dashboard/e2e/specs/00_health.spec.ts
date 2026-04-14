import { test, expect } from "../fixtures/dashboard";

test.describe("Cold launch smoke", () => {
  test("health endpoint responds OK", async ({ request }) => {
    const res = await request.get("/api/health");
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body).toHaveProperty("status");
  });

  test("home page loads", async ({ page, captureFullPage }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/LeRobot|Dashboard/i);
    await captureFullPage(page, "00_home");
  });
});
