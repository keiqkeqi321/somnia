import { defineConfig } from "@playwright/test";

const requestedChannel = process.env.PLAYWRIGHT_CHANNEL as "chrome" | "chromium" | "msedge" | undefined;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.ts",
  outputDir: "node_modules/.cache/playwright-results",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 800 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
  ],
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
    ...(requestedChannel ? { channel: requestedChannel } : {}),
  },
  webServer: [
    {
      command: "python ../../tests/remote_tracer_browser_fixture.py --relay-port 18787",
      url: "http://127.0.0.1:18787/health",
      timeout: 30_000,
      reuseExistingServer: false,
    },
    {
      command: "npm run build && npm run preview",
      url: "http://127.0.0.1:4173",
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
