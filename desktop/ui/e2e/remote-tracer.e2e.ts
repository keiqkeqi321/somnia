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
  await expect(page.getByLabel("Remote controls")).toBeVisible();
  await expect(page.getByLabel("Remote controls")).toContainText("Yolo and sensitive settings require confirmation on the computer.");
  await page.getByRole("button", { name: "New" }).click();
  await expect(page.locator(".remote-notice")).toContainText("is ready");

  const composer = page.getByPlaceholder("Ask Somnia");
  await composer.fill("/co");
  await expect(page.getByRole("listbox", { name: "Slash commands" })).toContainText("/compact");
  await page.getByRole("listbox", { name: "Slash commands" }).getByRole("button", { name: "/compact" }).click();
  await expect(composer).toHaveValue("/compact ");
  await composer.fill("@");
  await expect(page.getByRole("listbox", { name: "Project paths" })).toBeVisible();
  await composer.fill("line one");
  await composer.press("Shift+Enter");
  await expect(composer).toHaveValue("line one\n");
  await page.locator('input[type="file"]').setInputFiles({ name: "paste.png", mimeType: "image/png", buffer: Buffer.from("not-a-real-image") });
  await expect(page.locator(".remote-image-previews img")).toHaveCount(1);
  await page.getByRole("button", { name: "Remove paste.png" }).click();

  await composer.fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".remote-message-streaming p")).toHaveText("Hello ");
  await page.getByPlaceholder("Ask Somnia").fill("follow up");
  await page.getByRole("button", { name: "Queue" }).click();
  const queuedPrompts = page.getByLabel("Queued prompts");
  await expect(queuedPrompts).toContainText("follow up");
  await queuedPrompts.getByRole("button", { name: "Inject next loop" }).click();
  await expect(queuedPrompts).toContainText("Waiting for next loop");
  const completedMessage = page.locator(".remote-message-assistant:not(.remote-message-streaming)");
  await expect(completedMessage).toContainText("Hello remote");
  await expect(completedMessage.getByRole("heading", { name: "Rich output" })).toBeVisible();
  await expect(completedMessage.locator(".remote-code").first()).toContainText('print("remote")');
  await expect(completedMessage.locator(".remote-mermaid svg")).toBeVisible();
  await expect(completedMessage.getByRole("img", { name: "inline pixel" })).toBeVisible();
  await expect(page.locator(".remote-message-streaming")).toHaveCount(0);
  await expect(page.getByLabel("Execution progress")).toBeVisible();
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
