export interface RemoteDevice {
  device_id: string;
  name: string;
  created_at: number;
  revoked_at: number | null;
  status: "online" | "reconnecting" | "offline" | "revoked";
  projects: RemoteProject[];
}

export interface RemoteProject {
  project_id: string;
  name: string;
}

export interface PairingGrant {
  code: string;
  expires_at: number;
}

/** A third-party identity (e.g. GitHub) linked to the account, as reported by `GET /api/auth/identities`. */
export interface RemoteIdentity {
  provider: string;
  provider_user_id: string;
  provider_username: string;
  created_at: number;
}

/** Device-flow pair session as reported by `GET /api/pair-sessions/{id}`. */
export interface PairSessionInfo {
  status: "pending" | "approved" | "expired";
  code?: string;
  /** Device-side default for the Device name, e.g. the machine hostname. */
  suggested_name?: string;
}

type Fetcher = typeof fetch;

/** Error carrying the Relay HTTP status so callers can map failures (e.g. 409 vs 429 on register). */
export class RemoteRelayError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "RemoteRelayError";
    this.status = status;
  }
}

export class RemoteRelayClient {
  private readonly baseUrl: string;

  constructor(relayUrl: string, private readonly fetcher: Fetcher = (input, init) => fetch(input, init)) {
    this.baseUrl = relayHttpOrigin(relayUrl);
  }

  async login(username: string, password: string): Promise<void> {
    await this.request("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }, false);
  }

  async register(username: string, password: string): Promise<void> {
    await this.request("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }, false);
  }

  async listDevices(): Promise<RemoteDevice[]> {
    const payload = await this.request("/api/devices", {}, true) as { devices?: unknown };
    return Array.isArray(payload.devices) ? payload.devices as RemoteDevice[] : [];
  }

  /** Public relay metadata (`GET /api/info`): which OAuth channels the SPA may offer. */
  async getInfo(): Promise<{ webOrigin: string | null; oauthProviders: string[] }> {
    const payload = await this.request("/api/info", {}, false) as { web_origin?: unknown; oauth_providers?: unknown };
    return {
      webOrigin: typeof payload.web_origin === "string" ? payload.web_origin : null,
      oauthProviders: Array.isArray(payload.oauth_providers)
        ? payload.oauth_providers.filter((provider): provider is string => typeof provider === "string")
        : [],
    };
  }

  /**
   * Builds the OAuth authorize URL for a full-page redirect (the Relay 302s
   * to the provider). `redirect` is where the Relay sends the browser back.
   */
  oauthAuthorizeUrl(provider: string, mode: "login" | "bind", redirect: string): string {
    const query = new URLSearchParams({ mode, redirect });
    return `${this.baseUrl}/api/auth/${encodeURIComponent(provider)}/authorize?${query}`;
  }

  /** Completes the OAuth bind round trip with the grant from the callback fragment. */
  bindIdentity(provider: string, code: string, state: string): Promise<RemoteIdentity> {
    return this.request(`/api/auth/${encodeURIComponent(provider)}/bind`, {
      method: "POST",
      body: JSON.stringify({ code, state }),
    }, true) as Promise<RemoteIdentity>;
  }

  async listIdentities(): Promise<{ identities: RemoteIdentity[]; hasPassword: boolean }> {
    const payload = await this.request("/api/auth/identities", {}, true) as { identities?: unknown; has_password?: unknown };
    return {
      identities: Array.isArray(payload.identities) ? payload.identities as RemoteIdentity[] : [],
      hasPassword: payload.has_password === true,
    };
  }

  /**
   * Sets the password on a passwordless (OAuth-created) account, or changes an
   * existing one — then `currentPassword` is required by the Relay.
   */
  async setAccountPassword(password: string, currentPassword?: string): Promise<void> {
    const body = currentPassword ? { password, current_password: currentPassword } : { password };
    await this.request("/api/auth/password", {
      method: "POST",
      body: JSON.stringify(body),
    }, true);
  }

  async unbindIdentity(provider: string): Promise<void> {
    await this.request(`/api/auth/identities/${encodeURIComponent(provider)}`, { method: "DELETE" }, true);
  }

  async logout(): Promise<void> {
    await this.request("/api/auth/logout", { method: "POST" }, false);
  }

  createPairing(name: string): Promise<PairingGrant> {
    return this.request("/api/pairings", {
      method: "POST",
      body: JSON.stringify({ name }),
    }, true) as Promise<PairingGrant>;
  }

  /** Device-flow status check; unauthenticated — the secret in the query authorizes the read. */
  getPairSession(sessionId: string, secret: string): Promise<PairSessionInfo> {
    const query = new URLSearchParams({ secret });
    return this.request(`/api/pair-sessions/${encodeURIComponent(sessionId)}?${query}`, {}, false) as Promise<PairSessionInfo>;
  }

  /** Device-flow approval from the signed-in browser session. */
  async approvePairSession(sessionId: string, secret: string, deviceName: string): Promise<void> {
    await this.request(`/api/pair-sessions/${encodeURIComponent(sessionId)}/approve`, {
      method: "POST",
      body: JSON.stringify({ secret, device_name: deviceName }),
    }, true);
  }

  async revokeDevice(deviceId: string): Promise<void> {
    await this.request(`/api/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" }, true);
  }

  private async request(path: string, init: RequestInit, renew: boolean): Promise<unknown> {
    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init.headers },
    });
    if (response.status === 401 && renew) {
      const refreshed = await this.fetcher(`${this.baseUrl}/api/auth/refresh`, {
        method: "POST",
        credentials: "include",
      });
      if (refreshed.ok) {
        return this.request(path, init, false);
      }
    }
    const payload = await response.json().catch(() => ({})) as { error?: unknown };
    if (!response.ok) {
      throw new RemoteRelayError(String(payload.error ?? `Relay request failed (${response.status}).`), response.status);
    }
    return payload;
  }
}

function relayHttpOrigin(rawUrl: string): string {
  const url = new URL(String(rawUrl).trim());
  if (url.protocol === "ws:") url.protocol = "http:";
  if (url.protocol === "wss:") url.protocol = "https:";
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Relay URL must use http, https, ws, or wss.");
  }
  if (url.protocol === "http:" && !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("Relay HTTP is permitted only for loopback development; use HTTPS remotely.");
  }
  return url.toString().replace(/\/+$/, "");
}
