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

type Fetcher = typeof fetch;

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

  async listDevices(): Promise<RemoteDevice[]> {
    const payload = await this.request("/api/devices", {}, true) as { devices?: unknown };
    return Array.isArray(payload.devices) ? payload.devices as RemoteDevice[] : [];
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

  pairingLink(grant: PairingGrant): string {
    const url = new URL("somnia://pair");
    url.searchParams.set("relay", this.baseUrl);
    url.searchParams.set("code", grant.code);
    return url.toString();
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
      throw new Error(String(payload.error ?? `Relay request failed (${response.status}).`));
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
