# 17 — Desktop multi-project exposure over one connector

**What to build:** When remote control is enabled in Desktop, expose every project in the Desktop project list through the single embedded connector (not just the active workspace), so the web `#/connect` page lists all of them and the user can switch between them like CLI-registered projects.

**Blocked by:** 16 — Desktop one-click controlled device (completed).

**Status:** ready-for-agent

## Product/design decisions (2026-07-28)

- One connector per device, many projects: `RemoteConnector` already accepts a `sidecars` map and reports every project in `connector_presence`; the web project picker needs no changes.
- **No new process cost**: Desktop already runs one managed sidecar per opened project while the app is running, so multi-project exposure reuses existing processes. Lazy start / idle recycling is therefore *not* required here — it only matters for the CLI connector-managed mode (already handled there by `ProjectRuntimeManager`). A future change to Desktop's "start all project sidecars at launch" behavior is out of scope.
- The connector host remains the sidecar where remote control was enabled; project lifecycle (add/remove project in Desktop) should refresh the exposed set on next enable, and v2 may simply require a disable/enable cycle to apply changes.

## Backend (`desktop/backend/`)

- [x] `RemoteDeviceManager.enable` accepts a project list: `[{project_id, name, base_url}]` — own sidecar is the primary bridge, others go into `RemoteConnector(sidecars=..., project_names=...)`; single-project callers keep working.
- [x] Persist the project list in remote settings; autostart on sidecar launch re-exposes the persisted set (prune entries whose sidecar no longer answers, record in last_error).
- [x] `status()` reports the exposed projects.

## Frontend (Desktop)

- [x] Enabling remote control from Settings pushes the current Desktop project list (all managed sidecar connections: project id = existing `desktop-<hash>` scheme per workspace, name = project label, base_url from each managed connection) to the sidecar's `/remote/enable`.
- [x] Settings "Remote" section shows the exposed project list in status.
- [x] Project add/remove while enabled surfaces a hint that re-enabling applies the change (no live reconfiguration in v2).

## Verification

- Backend unit tests: multi-project enable builds one connector with the full bridge map; autostart prunes dead projects.
- `python -m unittest tests.test_desktop_remote tests.test_remote_connector`
- `cd desktop/ui && npm run typecheck && npm test -- --run && npm run build`
- Manual: Desktop with two projects, enable remote, web `#/connect` shows both projects, open a session in each.
