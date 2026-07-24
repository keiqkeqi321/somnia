import { expect, test } from "@playwright/test";

test("hosted browser streams a real Runtime turn and renders the reloaded Session", async ({ page }) => {
  await page.goto(
    "/?remote=1&relay=ws%3A%2F%2F127.0.0.1%3A18787&project=e2e-project",
  );

  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Device", { exact: true })).toContainText("Browser Test Device");
  await expect(page.getByLabel("Device", { exact: true })).toContainText("online");
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("e2e-project");
  await expect(page.getByLabel("Project", { exact: true })).toContainText("Browser test project");

  await page.getByLabel("New Device name").fill("Spare Device");
  await page.getByRole("button", { name: "Create pairing code" }).click();
  await expect(page.locator(".remote-pairing-code")).toHaveText(/^[A-Z2-9]{10}$/);

  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator(".remote-status")).toHaveText("connected");
  await page.getByRole("button", { name: "New" }).click();
  await expect(page.locator(".remote-notice")).toContainText("is ready");

  await page.getByPlaceholder("Ask Somnia").fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".remote-message-streaming p")).toHaveText("Hello ");
  await expect(page.locator(".remote-message-assistant:not(.remote-message-streaming) p")).toHaveText("Hello remote");
  await expect(page.locator(".remote-message-streaming")).toHaveCount(0);
  await page.getByRole("button", { name: "Archive" }).first().click();
  await expect(page.getByRole("button", { name: "Restore archived" })).toBeVisible();
  await page.getByRole("button", { name: "Restore archived" }).click();
  await expect(page.getByRole("button", { name: "Archive" }).first()).toBeVisible();
  const overflowing = await page.evaluate(() => Array.from(document.querySelectorAll("body *"))
    .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
    .map((element) => ({
      element: `${element.tagName.toLowerCase()}.${element.className}`,
      right: Math.round(element.getBoundingClientRect().right),
      width: Math.round(element.getBoundingClientRect().width),
      scrollWidth: element.scrollWidth,
    })));
  expect(overflowing).toEqual([]);
});
