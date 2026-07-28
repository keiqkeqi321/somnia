import { expect, test } from "@playwright/test";

test("hosted browser signs in through the remote routes and streams a real Runtime turn in the unified App UI", async ({ page }) => {
  // No hash: the router redirects to `#/login` while signed out.
  await page.goto("/?remote=1&relay=ws%3A%2F%2F127.0.0.1%3A18787");
  await expect(page).toHaveURL(/#\/login$/);

  // `#/login`: sign in against the Relay.
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/#\/connect$/);

  // `#/connect`: Device and Project pickers sourced from the paired Device.
  await expect(page.getByLabel("Device", { exact: true })).toContainText("Browser Test Device");
  await expect(page.getByLabel("Device", { exact: true })).toContainText("online");
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("e2e-project");
  await expect(page.getByLabel("Project", { exact: true })).toContainText("Browser test project");

  // Pairing stays available on the connect page.
  await page.getByLabel("New Device name").fill("Spare Device");
  await page.getByRole("button", { name: "Create pairing code" }).click();
  await expect(page.locator(".remote-pairing-code")).toHaveText(/^[A-Z2-9]{10}$/);

  // Connect into the unified App tree at `#/workspace`.
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page).toHaveURL(/#\/workspace$/);
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

  // Refreshing the `#/workspace` deep link restores the cookie session and
  // reconnects the last Device/Project automatically.
  await page.reload();
  await expect(page).toHaveURL(/#\/workspace$/);
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect(page.locator(".connection-dot")).toHaveAttribute("aria-label", "Connected");

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

test("registering a new account auto-signs in and lands on the device picker; duplicates are rejected", async ({ page }, testInfo) => {
  // The fixture Relay is shared across viewport projects and registration is
  // rate-limited per source, so each project registers exactly one new
  // account and reuses that same name for the duplicate-username check.
  const username = `e2e-reg-${testInfo.project.name}-${Math.random().toString(36).slice(2, 8)}`;

  await page.goto("/?remote=1&relay=ws%3A%2F%2F127.0.0.1%3A18787");
  await expect(page).toHaveURL(/#\/login$/);

  // `#/login` links to `#/register`; both are legal while signed out.
  await page.getByRole("link", { name: "No account yet? Register" }).click();
  await expect(page).toHaveURL(/#\/register$/);

  // Client-side validation: mismatched/short passwords never reach the Relay.
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password", { exact: true }).fill("e2e-remote-password");
  await page.getByLabel("Confirm password").fill("e2e-remote-password-2");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.locator(".remote-notice")).toContainText("Passwords do not match.");
  await page.getByLabel("Password", { exact: true }).fill("short");
  await page.getByLabel("Confirm password").fill("short");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.locator(".remote-notice")).toContainText("Password must be at least 8 characters.");

  // Happy path: register → auto sign-in → `#/connect` with the device picker.
  await page.getByLabel("Password", { exact: true }).fill("e2e-remote-password");
  await page.getByLabel("Confirm password").fill("e2e-remote-password");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/#\/connect$/);
  await expect(page.getByLabel("Device", { exact: true })).toBeVisible();

  // Duplicate username (case-insensitive) is rejected with a 409 notice. The
  // Relay counts every registration attempt toward the 5/hour per-source
  // limit, so only one viewport project spends the extra attempt (4 projects
  // × 1 register + 1 duplicate = 5 attempts, exactly at the limit).
  if (testInfo.project.name === "phone") {
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/#\/login$/);
    await page.getByRole("link", { name: "No account yet? Register" }).click();
    await expect(page).toHaveURL(/#\/register$/);
    await page.getByLabel("Username").fill(username.toUpperCase());
    await page.getByLabel("Password", { exact: true }).fill("e2e-remote-password");
    await page.getByLabel("Confirm password").fill("e2e-remote-password");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.locator(".remote-notice")).toContainText("Username is already taken.");
    await expect(page).toHaveURL(/#\/register$/);
  }
});
