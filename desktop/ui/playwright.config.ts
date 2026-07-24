import { defineConfig } from "@playwright/test";

const requestedChannel = process.env.PLAYWRIGHT_CHANNEL as "chrome" | "chromium" | "msedge" | undefined;
const uiPort = Number(process.env.PLAYWRIGHT_UI_PORT ?? "4173");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.ts",
  outputDir: "node_modules/.cache/playwright-results",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "phone", use: { viewport: { width: 390, height: 844 } } },
    { name: "tablet", use: { viewport: { width: 768, height: 1024 } } },
    { name: "laptop", use: { viewport: { width: 1280, height: 800 } } },
    { name: "wide-desktop", use: { viewport: { width: 1920, height: 1080 } } },
  ],
  use: {
    baseURL: `http://127.0.0.1:${uiPort}`,
    headless: true,
    screenshot: "on",
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
      command: `npm run build && python scripts/preview_server.py --port ${uiPort}`,
      url: `http://127.0.0.1:${uiPort}`,
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
