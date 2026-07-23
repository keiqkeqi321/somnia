import { useRef, useState } from "react";

import { RemoteRelayClient, type RemoteDevice } from "./remote-relay-client";

export function useRemoteAccess(initialRelayUrl: string) {
  const [relayUrl, setRelayUrl] = useState(initialRelayUrl);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [devices, setDevices] = useState<RemoteDevice[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [pairingName, setPairingName] = useState("");
  const [pairingCode, setPairingCode] = useState("");
  const [notice, setNotice] = useState("Sign in to Somnia Remote.");
  const [busy, setBusy] = useState(false);
  const clientRef = useRef<RemoteRelayClient | null>(null);

  async function signIn(): Promise<void> {
    setBusy(true);
    try {
      const client = new RemoteRelayClient(relayUrl);
      await client.login(username.trim(), password);
      const availableDevices = await client.listDevices();
      clientRef.current = client;
      setDevices(availableDevices);
      selectFirstActiveDevice(availableDevices);
      setAuthenticated(true);
      setPassword("");
      setNotice("Signed in.");
    } catch (error) {
      setNotice(formatError(error));
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
      setDevices(availableDevices);
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

  async function signOut(): Promise<void> {
    setBusy(true);
    try {
      await clientRef.current?.logout();
    } catch (error) {
      setNotice(formatError(error));
    } finally {
      clientRef.current = null;
      setAuthenticated(false);
      setDevices([]);
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
    busy,
    createPairing,
    deviceId,
    devices,
    notice,
    pairingCode,
    pairingName,
    password,
    relayUrl,
    revokeSelectedDevice,
    setDeviceId,
    setNotice,
    setPairingName,
    setPassword,
    setRelayUrl,
    setUsername,
    signIn,
    signOut,
    username,
    verifyAccess,
  };
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
