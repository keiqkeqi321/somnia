import { useEffect, useRef, useState } from "react";

import { oauthProviderLabel, spaSelfUrl } from "./remote-oauth";
import { RemoteRelayClient, RemoteRelayError, type RemoteDevice, type RemoteIdentity } from "./remote-relay-client";

export function useRemoteAccess(initialRelayUrl: string) {
  const [relayUrl, setRelayUrl] = useState(initialRelayUrl);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [devices, setDevices] = useState<RemoteDevice[]>([]);
  const [identities, setIdentities] = useState<RemoteIdentity[]>([]);
  const [oauthProviders, setOauthProviders] = useState<string[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [pairingName, setPairingName] = useState("");
  const [pairingCode, setPairingCode] = useState("");
  const [notice, setNotice] = useState("Sign in to Somnia Remote.");
  const [busy, setBusy] = useState(false);
  const clientRef = useRef<RemoteRelayClient | null>(null);
  // Mirrors `devices` so async flows started from a stale render (e.g. the
  // refresh-restore effect on mount) still see the latest device list.
  const devicesRef = useRef<RemoteDevice[]>([]);

  // Which OAuth channels the Relay has configured (public `/api/info`) —
  // drives the per-channel sign-in and bind buttons. Best-effort: a failed
  // or unreachable Relay just leaves the list empty.
  useEffect(() => {
    let cancelled = false;
    async function loadOauthProviders() {
      try {
        const info = await new RemoteRelayClient(relayUrl).getInfo();
        if (!cancelled) {
          setOauthProviders(info.oauthProviders);
        }
      } catch {
        // Silent: an old or unreachable Relay simply offers no channels.
      }
    }
    void loadOauthProviders();
    return () => {
      cancelled = true;
    };
  }, [relayUrl]);

  function applyDevices(availableDevices: RemoteDevice[]) {
    devicesRef.current = availableDevices;
    setDevices(availableDevices);
  }

  /**
   * Self-service registration (kept for the hidden `#/register` deep link):
   * the Relay issues the same cookie session as login on success, so the
   * flow ends authenticated exactly like a restored session.
   */
  async function signUp(): Promise<void> {
    setBusy(true);
    try {
      const client = new RemoteRelayClient(relayUrl);
      await client.register(username.trim(), password);
      const availableDevices = await client.listDevices();
      clientRef.current = client;
      applyDevices(availableDevices);
      selectFirstActiveDevice(availableDevices);
      setIdentities((await client.listIdentities()).identities);
      setAuthenticated(true);
      setPassword("");
      setNotice("Account created. Signed in.");
    } catch (error) {
      setNotice(formatRegisterError(error));
    } finally {
      setBusy(false);
    }
  }

  /**
   * Re-attaches to an existing cookie session after a page refresh: the relay
   * cookie is still in the browser, so listing Devices doubles as the session
   * check. Returns false when the session expired (caller routes to login).
   */
  async function restoreSession(): Promise<boolean> {
    setBusy(true);
    try {
      const client = new RemoteRelayClient(relayUrl);
      const availableDevices = await client.listDevices();
      clientRef.current = client;
      applyDevices(availableDevices);
      selectFirstActiveDevice(availableDevices);
      setIdentities((await client.listIdentities()).identities);
      setAuthenticated(true);
      setNotice("Signed in.");
      return true;
    } catch {
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function createPairing(): Promise<void> {
    const client = clientRef.current;
    const name = pairingName.trim();
    if (!client || !name) return;
    setBusy(true);
    try {
      const pairing = await client.createPairing(name);
      setPairingCode(pairing.code);
      setPairingName("");
      setNotice(`Pairing code expires at ${new Date(pairing.expires_at * 1000).toLocaleTimeString()}.`);
    } catch (error) {
      setNotice(formatError(error));
    } finally {
      setBusy(false);
    }
  }

  async function revokeSelectedDevice(): Promise<boolean> {
    const client = clientRef.current;
    if (!client || !deviceId) return false;
    setBusy(true);
    try {
      await client.revokeDevice(deviceId);
      const availableDevices = await client.listDevices();
      applyDevices(availableDevices);
      selectFirstActiveDevice(availableDevices);
      setNotice("Device revoked.");
      return true;
    } catch (error) {
      setNotice(formatError(error));
      return false;
    } finally {
      setBusy(false);
    }
  }

  /**
   * OAuth sign-in: full-page redirect to the Relay authorize endpoint (mode
   * `login`). On success the Relay sets the same cookie session and lands back
   * on the bare SPA address, where the mount restore picks the session up.
   */
  function beginOAuthSignIn(provider: string): void {
    const client = new RemoteRelayClient(relayUrl);
    window.location.assign(client.oauthAuthorizeUrl(provider, "login", spaSelfUrl()));
  }

  async function completeOAuthBind(grant: { provider: string; code: string; state: string }): Promise<void> {
    const client = clientRef.current;
    if (!client) return;
    setBusy(true);
    try {
      await client.bindIdentity(grant.provider, grant.code, grant.state);
      setIdentities((await client.listIdentities()).identities);
      setNotice(`${oauthProviderLabel(grant.provider)} account bound.`);
    } catch (error) {
      setNotice(formatError(error));
    } finally {
      setBusy(false);
    }
  }

  async function signOut(): Promise<void> {
    setBusy(true);
    try {
      await clientRef.current?.logout();
    } catch (error) {
      setNotice(formatError(error));
    } finally {
      clientRef.current = null;
      devicesRef.current = [];
      setAuthenticated(false);
      setDevices([]);
      setIdentities([]);
      setDeviceId("");
      setPairingCode("");
      setBusy(false);
    }
  }

  async function verifyAccess(): Promise<boolean> {
    try {
      await clientRef.current?.listDevices();
      return clientRef.current !== null;
    } catch (error) {
      setNotice(formatError(error));
      return false;
    }
  }

  function selectFirstActiveDevice(availableDevices: RemoteDevice[]) {
    setDeviceId(availableDevices.find((device) => !device.revoked_at)?.device_id ?? "");
  }

  return {
    authenticated,
    beginOAuthSignIn,
    busy,
    completeOAuthBind,
    createPairing,
    deviceId,
    devices,
    devicesRef,
    identities,
    notice,
    oauthProviders,
    pairingCode,
    pairingName,
    password,
    relayUrl,
    restoreSession,
    revokeSelectedDevice,
    setDeviceId,
    setNotice,
    setPairingName,
    setPassword,
    setRelayUrl,
    setUsername,
    signOut,
    signUp,
    username,
    verifyAccess,
  };
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Maps registration failures to notice copy. 400 keeps the backend message
 * (it names the credential-policy violation); the rest get stable text.
 */
function formatRegisterError(error: unknown): string {
  if (error instanceof RemoteRelayError) {
    if (error.status === 400) {
      return error.message;
    }
    if (error.status === 403) {
      return "Registration is disabled on this Relay.";
    }
    if (error.status === 409) {
      return "Username is already taken.";
    }
    if (error.status === 429) {
      return "Too many registration attempts. Try again later.";
    }
  }
  return formatError(error);
}
