/**
 * localStorage namespacing for Remote mode.
 *
 * Desktop-mode keys (`somnia.desktop.*`) are global and never change shape.
 * Remote-mode browser state is bucketed per Device/Project under
 * `somnia.remote.<kind>:<deviceId>:<projectId>`, so remote memory never
 * collides with — or leaks into — desktop keys.
 */

export const REMOTE_PROJECT_PATH_PREFIX = "remote://";
export const REMOTE_STORAGE_PREFIX = "somnia.remote";

export interface RemoteProjectBucket {
  deviceId: string;
  projectId: string;
}

export function isRemoteProjectPath(projectPath: string | null | undefined): boolean {
  return String(projectPath ?? "").startsWith(REMOTE_PROJECT_PATH_PREFIX);
}

export function remoteProjectPath(deviceId: string, projectId: string): string {
  return `${REMOTE_PROJECT_PATH_PREFIX}${deviceId}/${projectId}`;
}

/**
 * Parses `remote://<deviceId>/<projectId>` into its bucket. The project id
 * may itself contain slashes; only the first segment is the device id.
 */
export function parseRemoteProjectBucket(projectPath: string | null | undefined): RemoteProjectBucket | null {
  const raw = String(projectPath ?? "");
  if (!raw.startsWith(REMOTE_PROJECT_PATH_PREFIX)) {
    return null;
  }
  const rest = raw.slice(REMOTE_PROJECT_PATH_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0 || slash >= rest.length - 1) {
    return null;
  }
  return { deviceId: rest.slice(0, slash), projectId: rest.slice(slash + 1) };
}

/**
 * Returns the remote-bucketed storage key for `kind`, or `null` when the
 * project path is not a remote project (callers fall back to the desktop
 * key in that case).
 */
export function remoteScopedStorageKey(kind: string, projectPath: string | null | undefined): string | null {
  const bucket = parseRemoteProjectBucket(projectPath);
  if (!bucket) {
    return null;
  }
  return `${REMOTE_STORAGE_PREFIX}.${kind}:${bucket.deviceId}:${bucket.projectId}`;
}

const REMOTE_LAST_TARGET_KEY = `${REMOTE_STORAGE_PREFIX}.last-target`;

/**
 * Remembers the last connected Device/Project so a refresh on `#/workspace`
 * can restore the connection while the relay cookie session is still valid.
 * Cleared on "switch target" and sign-out.
 */
export function readRemoteLastTarget(): RemoteProjectBucket | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const value = JSON.parse(window.localStorage.getItem(REMOTE_LAST_TARGET_KEY) ?? "null") as unknown;
    if (!value || typeof value !== "object") {
      return null;
    }
    const { deviceId, projectId } = value as { deviceId?: unknown; projectId?: unknown };
    if (typeof deviceId !== "string" || typeof projectId !== "string" || !deviceId || !projectId) {
      return null;
    }
    return { deviceId, projectId };
  } catch {
    return null;
  }
}

export function writeRemoteLastTarget(bucket: RemoteProjectBucket | null): void {
  if (typeof window === "undefined") {
    return;
  }
  if (!bucket) {
    window.localStorage.removeItem(REMOTE_LAST_TARGET_KEY);
    return;
  }
  window.localStorage.setItem(REMOTE_LAST_TARGET_KEY, JSON.stringify(bucket));
}
