import { expect, test } from "@playwright/test";

test("hosted browser signs in through RemoteGate and streams a real Runtime turn in the unified App UI", async ({ page }) => {
  await page.goto(
    "/?remote=1&relay=ws%3A%2F%2F127.0.0.1%3A18787&project=e2e-project",
  );

  // RemoteGate: sign in against the Relay.
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  // RemoteGate: Device and Project pickers sourced from the paired Device.
  await expect(page.getByLabel("Device", { exact: true })).toContainText("Browser Test Device");
  await expect(page.getByLabel("Device", { exact: true })).toContainText("online");
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("e2e-project");
  await expect(page.getByLabel("Project", { exact: true })).toContainText("Browser test project");

  // Pairing stays available in the gate.
  await page.getByLabel("New Device name").fill("Spare Device");
  await page.getByRole("button", { name: "Create pairing code" }).click();
  await expect(page.locator(".remote-pairing-code")).toHaveText(/^[A-Z2-9]{10}$/);

  // Connect into the unified App tree.
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect(page.locator(".connection-dot")).toHaveAttribute("aria-label", "Connected");
  // Remote chrome: the sidebar offers a switch-target button instead of local project creation.
  await expect(page.getByLabel("Switch device / project")).toBeVisible();

  // Under the new policy Yolo can be enabled remotely, so the mode picker lists it.
  await page.locator(".mode-pill").click();
  await expect(page.locator(".mode-picker-panel").getByRole("button", { name: /Yolo/ })).toBeVisible();
  await page.locator(".mode-pill").click();

  // Sessions are created through the project menu in the unified sidebar.
  // (The fixture Relay/Sidecar is shared across viewport projects, so session
  // cards from earlier projects may accumulate — always scope to the selected card.)
  await page.getByLabel("Project options for Browser test project").click();
  await page.getByRole("button", { name: "New", exact: true }).click();
  const selectedCard = page.locator(".session-card.selected");
  await expect(selectedCard).toBeVisible();

  const composer = page.locator(".composer textarea");
  await composer.fill("/sy");
  await expect(page.locator(".command-picker")).toContainText("/symbols");
  await page.locator(".command-picker").getByRole("button", { name: /\/symbols/ }).click();
  await expect(composer).toHaveValue("/symbols ");
  await composer.fill("@");
  await expect(page.locator(".command-picker.path-picker")).toBeVisible();
  await composer.fill("line one");
  await composer.press("Shift+Enter");
  await expect(composer).toHaveValue("line one\n");
  await page.locator('input[type="file"]').setInputFiles({ name: "paste.png", mimeType: "image/png", buffer: Buffer.from("not-a-real-image") });
  await expect(page.locator(".pending-attachments img")).toHaveCount(1);
  await page.getByTitle("Remove paste.png").click();
  await expect(page.locator(".pending-attachments img")).toHaveCount(0);

  // Send a prompt; the shared conversation view renders the streaming turn.
  await composer.fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".bubble.user")).toContainText("hello");
  await expect(page.locator(".bubble.assistant")).toContainText("Hello ");
  await expect(page.locator(".conversation-answering-indicator")).toBeVisible();

  // While the turn runs, sending again queues the prompt for the session.
  await composer.fill("follow up");
  await page.getByRole("button", { name: "Send" }).click();
  const queuedPrompts = page.locator(".prompt-queue-card");
  await expect(queuedPrompts).toContainText("follow up");
  await queuedPrompts.locator(".queue-inject-button").click();
  await expect(queuedPrompts.locator(".queue-inject-button")).toBeDisabled();

  // The completed turn renders through the shared Markdown/Mermaid pipeline.
  const completedMessage = page.locator(".bubble.assistant").last();
  await expect(completedMessage).toContainText("Hello remote");
  await expect(completedMessage.getByRole("heading", { name: "Rich output" })).toBeVisible();
  await expect(completedMessage.locator(".markdown-code-block").first()).toContainText('print("remote")');
  await expect(completedMessage.locator(".mermaid-card svg")).toBeVisible();
  // App renders inline Markdown images as links; the data URL still reaches the conversation.
  await expect(completedMessage.locator('a[href^="data:image/png"]')).toBeVisible();

  // Archive through the session menu, restore through Settings → Archived threads.
  const sessionMenuTrigger = selectedCard.getByLabel(/Session options for /);
  await expect(sessionMenuTrigger).toBeVisible();
  const sessionId = (await sessionMenuTrigger.getAttribute("aria-label"))!.replace("Session options for ", "");
  await sessionMenuTrigger.click();
  await page.getByRole("button", { name: "Archive", exact: true }).click();
  await expect(page.getByLabel(`Session options for ${sessionId}`)).toHaveCount(0);
  await page.getByLabel("Settings", { exact: true }).click();
  await page.getByRole("button", { name: "Archived threads" }).click();
  await page.locator(".archived-row", { hasText: sessionId }).getByRole("button", { name: "Restore", exact: true }).click();
  await page.locator(".settings-back").click();
  await expect(page.getByLabel(`Session options for ${sessionId}`)).toHaveCount(1);

  // The .ambient backdrop glows are intentionally oversized and clipped by .shell;
  // exclude them from the horizontal-overflow audit.
  const overflowing = await page.evaluate(() => Array.from(document.querySelectorAll("body *"))
    .filter((element) => !element.classList.contains("ambient"))
    .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
    .map((element) => ({
      element: `${element.tagName.toLowerCase()}.${element.className}`,
      right: Math.round(element.getBoundingClientRect().right),
      width: Math.round(element.getBoundingClientRect().width),
      scrollWidth: element.scrollWidth,
    })));
  expect(overflowing).toEqual([]);
});
