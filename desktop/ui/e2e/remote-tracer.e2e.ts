import { expect, test } from "@playwright/test";

test("hosted browser streams a real Runtime turn and renders the reloaded Session", async ({ page }) => {
  await page.goto(
    "/?remote=1&relay=ws%3A%2F%2F127.0.0.1%3A18787&device=e2e-device&project=e2e-project",
  );

  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator(".remote-status")).toHaveText("connected");
  await page.getByRole("button", { name: "New" }).click();
  await expect(page.getByRole("status")).toContainText("is ready");

  await page.getByPlaceholder("Ask Somnia").fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".remote-message-streaming p")).toHaveText("Hello ");
  await expect(page.locator(".remote-message-assistant:not(.remote-message-streaming) p")).toHaveText("Hello remote");
  await expect(page.locator(".remote-message-streaming")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
