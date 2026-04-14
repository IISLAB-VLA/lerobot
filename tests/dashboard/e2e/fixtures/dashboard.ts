import { test as base, expect, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";

export interface DashboardFixtures {
  screenshotDir: string;
  captureFullPage: (page: Page, name: string) => Promise<string>;
}

const SCREENSHOT_ROOT = resolve(__dirname, "../screenshots");

export const test = base.extend<DashboardFixtures>({
  screenshotDir: async ({}, use) => {
    await mkdir(SCREENSHOT_ROOT, { recursive: true });
    await use(SCREENSHOT_ROOT);
  },
  captureFullPage: async ({ screenshotDir }, use) => {
    const capture = async (page: Page, name: string) => {
      const filePath = resolve(screenshotDir, `${name}.png`);
      await mkdir(dirname(filePath), { recursive: true });
      await page.screenshot({ path: filePath, fullPage: true });
      return filePath;
    };
    await use(capture);
  },
});

export { expect };
