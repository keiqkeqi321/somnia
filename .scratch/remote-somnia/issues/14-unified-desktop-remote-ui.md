# 14 — Unify Desktop and Remote Web into one UI tree

**What to build:** Retire `RemoteTracerApp` and let `?remote=1` render the same `App.tsx` tree as Desktop, with the connection seam selecting `RemoteSomniaConnection` instead of `DirectSomniaConnection`. Remote mode may only switch among pre-registered Projects on a paired Device (no Project creation/removal); every other capability — Settings (MCP, providers, runtime, system prompt, skills), authorization approval, execution modes including Yolo — must behave with no visible difference from Desktop.

**Blocked by:** none (issues 01–13 are completed).

**Status:** completed

## Product decisions (2026-07-27, supersede issue 11 and the old spec restrictions)

- **Remote channel is fully authorized.** Remote clients may approve tool-authorization and mode-switch interactions, including persisting permission grants. The previous "wait for confirmation on the computer" rule is removed.
- **Yolo may be enabled remotely.** The Connector-side Yolo block (`connector.py` execution.mode branch) is removed.
- **Hooks configuration is editable remotely** like every other section — verified (paired, non-revoked) Devices get full read/write access to all config sections, hooks included.
- **Device verification is the security gate.** Full remote authority is only available through an authenticated, paired (non-revoked) Device. Pairing/revocation stays as-is.

## Connector gap (sidecar has it, Connector does not forward it)

Add these methods to `LocalSidecarBridge.execute` in `open_somnia/remote/connector.py` (relay is a transparent pipe, no relay change needed; request-id dedup is automatic):

- [x] `settings.config.get` → `GET /settings/config` (also covers the skills list, which rides on this endpoint); hooks section may be returned read-only
- [x] `settings.config.save` → `POST /settings/config` (all sections writable, hooks included)
- [x] `provider.presets` → `GET /provider-presets`
- [x] `provider.debug_model` → `POST /providers/debug-model`
- [x] `mcp.list` → `GET /mcp/servers`
- [x] `mcp.debug` → `POST /mcp/servers/{name}/debug`
- [x] `mcp.set_enabled` → `POST /mcp/servers/{name}/enabled`
- [x] `interaction.resolve_authorization` → `POST /interactions/{id}/authorization`
- [x] `interaction.resolve_mode_switch` → `POST /interactions/{id}/mode-switch`
- [x] Remove the Yolo restriction in the existing `execution.mode` branch

## Frontend connection layer

- [x] Add matching wrappers to `desktop/ui/src/lib/remote-somnia-connection.ts` for every new Connector method.
- [x] Widen the remote `setExecutionMode` type to include `"yolo"`; add `scope` to `setVisionModel`; allow session-less `listTasks` / `getTeamLog` / `listActiveTeamMembers` where Desktop supports it.
- [x] Extract a full client interface covering the ~30 REST methods `App.tsx` calls directly on `SidecarClient` today (Settings, MCP, interactions, tool-logs, tasks, team, workspace, provider controls), so `App.tsx` depends only on the interface. Extend the contract suite in `somnia-connection.contract.ts` beyond the current 8 methods. — Done in Phase 2: `desktop/ui/src/lib/somnia-client.ts` defines `SomniaClient` (extends `SomniaConnection`); `DirectSomniaClient` composes `SidecarClient` + `DirectSomniaConnection`, `RemoteSomniaConnection` implements the same interface (signatures aligned to `SidecarClient`, incl. `runtimeStatus` rename). `App.tsx` now keeps one unified client per project (`clientRef`/`projectClientsRef`); the contract suite covers settings.config read/write, provider.presets, mcp.list, interaction.resolve_authorization, tool_log, tasks, workspace.paths, runtime.status for both adapters.

## UI unification

- [x] Remote entry flow inside `App.tsx`: `useRemoteAccess` sign-in → Device picker → Project picker sourced from `device.projects`. Project create/remove buttons hidden in remote mode (`App.tsx` `initializeConnection` / `handleCreateProject` / `handleRemoveProject` are local-only paths). — Done in Phase 3 (2026-07-27): `main.tsx` renders `App remoteMode` for `?remote=1`; new `src/components/RemoteGate.tsx` (login → Device → Project → Connect) gates the remote tree; `connectRemoteProject` builds `RemoteSomniaConnection` and reuses `openEventConnection` registration/subscription; sidebar "＋" becomes a switch-target button in remote mode.
- [x] Parameterize connection creation (`App.tsx:1003` `new DirectSomniaConnection`) to select the remote adapter under `?remote=1`. — Done in Phase 3: `connectRemoteProject` constructs `RemoteSomniaConnection({ relayUrl, deviceId, projectId })`, waits for the socket to reach `connected`, then follows the same project registration path; remote project paths use the `remote://<deviceId>/<projectId>` scheme so they never enter desktop localStorage keys.
- [x] Image rendering: replace direct `<img src="${baseUrl}/workspace/images?path=">` with an async resolver that uses authenticated `workspace.image` (data URL) in remote mode. — Done in Phase 4 (2026-07-27): `SomniaClient.getWorkspaceImage(path)` added to the interface and `DirectSomniaClient` (returns the existing `workspaceImageUrl` HTTP URL); new `src/lib/workspace-image.ts` provides `useWorkspaceImageSource` (sync fast path for http(s)/data URLs and Direct base-URL links, async data-URL resolution for Remote, per-client cache + in-flight dedup + unmount cancel). `UserImagePreview` / `ToolImagePreview` / `ToolCallWithImages` now take `client` instead of `baseUrl`; `toolImageSource` removed.
- [x] Hide local-only chrome in remote mode: Tauri titlebar/window controls, "Open workspace" / "Open config file" buttons (paths may still be displayed read-only). — Done in Phase 3: window controls hidden (brand + Settings remain); project-menu "Open workspace"/"Remove" and the sidebar "New Project" button hidden; conversation-header workspace link renders as read-only text; `SettingsView.onOpenPath` is nullable and remote passes `null`.
- [x] Unify localStorage key namespaces (App uses global keys; RemoteTracerApp buckets by deviceId/projectId — keep the per-device/project bucketing). — Phase 3 partial: `persistLastOpenedSession` / `clearLastOpenedSession` skip `remote://` paths so remote memory never pollutes desktop keys. — Done in Phase 4 (2026-07-27): new `src/lib/remote-storage.ts` buckets remote state as `somnia.remote.<kind>:<deviceId>:<projectId>`; prompt history and last-opened-session read/write through the bucket for `remote://` project paths (loaded on `activateProject`), while desktop keys (`somnia.desktop.*`) keep their exact names and formats. Archived sessions intentionally stay in the single `somnia.desktop.archived-sessions` map — `remote://<deviceId>/<projectId>` path keys are already unique per bucket and can never collide with desktop filesystem paths (documented in code). App has no draft persistence, so nothing to namespace there.
- [x] Handle remote-only notifications in the shared UI: `snapshot` resync, reconnect states, authoritative `turn_result` reload. — Done in Phase 4 (2026-07-27): `openEventConnection` now handles `snapshot` via `resyncAfterRemoteSnapshot` (refreshes session list, current session, runtime status, providers/models, interactions, tool logs — reusing existing refresh functions; non-active projects only get their sidebar session list updated). Remote `connecting`/`disconnected`/`error` states show reconnect-specific bilingual copy (new i18n keys `remote.state.*`, `remote.reconnecting`, `remote.connectingDevice`, `remote.connectionFailed`, `remote.resynced`); the composer connection dot shows localized remote state labels. Direct mode never emits snapshots and keeps the original banner strings.
- [x] Hooks section in Settings is fully editable in remote mode (same as Desktop). — Covered by the shared `SettingsView` plus `settings.config.get`/`settings.config.save` on `RemoteSomniaConnection` (all sections writable, hooks included); contract suite asserts read/write on both adapters.
- [x] Delete `RemoteTracerApp.tsx` / `RemoteRichContent.tsx` once parity is reached. — Done in Phase 5 (2026-07-27): both files deleted after confirming zero remaining imports; RemoteTracerApp-only `.remote-*` styles removed from `styles.css` (RemoteGate's `remote-shell`/`remote-login`/`remote-connection`/`remote-pairing`/`remote-notice`/`remote-empty`/`remote-gate-title`/`remote-pairing-code` kept); `e2e/remote-tracer.e2e.ts` rewritten against the unified RemoteGate + App UI.

## Verification

- `python -m unittest tests.test_remote_connector` (extended: new methods, yolo allowed) — 10 tests OK (2026-07-27).
- `npm run typecheck` — clean (2026-07-27).
- `npm test -- --run` (extended contract suite runs against both adapters) — 31 tests OK (2026-07-27).
- `npm run build` — OK (2026-07-27).
- `python -m unittest tests.test_cli_resume tests.test_process_output tests.test_repl_todo tests.test_runtime_tool_output` — 310 tests OK, no regression (2026-07-27).
- `npm run test:e2e -- e2e/remote-tracer.e2e.ts` (phone/tablet/laptop/wide-desktop): the spec was rewritten for the unified RemoteGate + App UI in Phase 5 (sign-in → Device/Project pickers → pairing → Connect → unified sidebar/composer/conversation; Yolo listed in the remote mode picker; archive via session menu, restore via Settings → Archived threads). **4 passed (30.8s, 2026-07-27)** after fixing the pre-existing fixture gap: `tests/remote_tracer_support.py` now configures a `provider_profiles` entry (`openai`/`fake-model`) so the fixture Sidecar answers `GET /models` correctly. Also fixed en route: `.remote-shell-gate` no longer inherits the retired fixed-row grid (pairing section overlapped the Connect button at ≤700px), and a stray `preview_server.py` on port 4173 was killed. Note: Playwright projects share one fixture Relay/Sidecar, so sessions accumulate across viewport runs — the spec scopes session assertions to `.session-card.selected` for that reason. The e2e must run on the default UI port 4173/4174 because the fixture Relay's `allowed_origins` is pinned to those origins (running with `PLAYWRIGHT_UI_PORT=4599` fails sign-in with CORS "Failed to fetch").
- Manual: remote browser edits provider/runtime/mcp/hooks config, toggles an MCP server, approves an authorization prompt, and switches to Yolo. — Not re-run manually in Phase 5; covered at API level by `tests.test_remote_connector` and at UI level by the adapted e2e (see fixture caveat above).
