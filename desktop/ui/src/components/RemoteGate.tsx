import { useEffect, useState } from "react";

import { useI18n } from "../lib/i18n";
import type { useRemoteAccess } from "../lib/use-remote-access";

type RemoteAccess = ReturnType<typeof useRemoteAccess>;

type RemoteGateProps = {
  access: RemoteAccess;
  connecting: boolean;
  onConnect: (deviceId: string, projectId: string) => void;
};

/**
 * Remote-mode entry gate rendered inside App when `?remote=1`: sign in against
 * the relay, pick a paired Device, then pick one of the Device's registered
 * projects. Remote mode can only switch among pre-registered projects — there
 * is intentionally no project create/remove UI here.
 */
export default function RemoteGate({ access, connecting, onConnect }: RemoteGateProps) {
  const { t } = useI18n();
  const [projectId, setProjectId] = useState("");

  const busy = access.busy || connecting;
  const selectedDevice = access.devices.find((device) => device.device_id === access.deviceId);
  const projects = selectedDevice?.projects ?? [];

  useEffect(() => {
    if (projects.length > 0 && !projects.some((project) => project.project_id === projectId)) {
      setProjectId(projects[0].project_id);
    } else if (projects.length === 0 && projectId) {
      setProjectId("");
    }
  }, [projectId, projects]);

  if (!access.authenticated) {
    return (
      <main className="remote-shell remote-shell-login">
        <form
          className="remote-login"
          onSubmit={(event) => {
            event.preventDefault();
            void access.signIn();
          }}
        >
          <h1>{t("remote.title")}</h1>
          <label>
            {t("remote.relay")}
            <input value={access.relayUrl} onChange={(event) => access.setRelayUrl(event.target.value)} />
          </label>
          <label>
            {t("remote.username")}
            <input value={access.username} onChange={(event) => access.setUsername(event.target.value)} autoComplete="username" />
          </label>
          <label>
            {t("remote.password")}
            <input
              type="password"
              value={access.password}
              onChange={(event) => access.setPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          <button type="submit" disabled={busy || !access.username.trim() || !access.password}>
            {t("remote.signIn")}
          </button>
          <div className="remote-notice" role="status">
            {access.notice}
          </div>
        </form>
      </main>
    );
  }

  return (
    <main className="remote-shell remote-shell-gate">
      <section className="remote-connection" aria-label={t("remote.title")}>
        <h1 className="remote-gate-title">{t("remote.title")}</h1>
        <label>
          {t("remote.device")}
          <select
            aria-label={t("remote.device")}
            value={access.deviceId}
            onChange={(event) => access.setDeviceId(event.target.value)}
            disabled={busy}
          >
            <option value="">{t("remote.selectDevice")}</option>
            {access.devices
              .filter((device) => !device.revoked_at)
              .map((device) => (
                <option key={device.device_id} value={device.device_id}>
                  {device.name} ({device.status})
                </option>
              ))}
          </select>
        </label>
        <label>
          {t("remote.project")}
          <select
            aria-label={t("remote.project")}
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
            disabled={busy || projects.length === 0}
          >
            <option value="">{t("remote.selectProject")}</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
        {selectedDevice && projects.length === 0 ? <p className="remote-empty">{t("remote.noProjects")}</p> : null}
        <button
          type="button"
          onClick={() => onConnect(access.deviceId, projectId)}
          disabled={!access.deviceId || !projectId || selectedDevice?.status !== "online" || busy}
        >
          {connecting ? t("sidebar.connecting") : t("remote.connect")}
        </button>
        <button type="button" onClick={() => void access.revokeSelectedDevice()} disabled={!access.deviceId || busy}>
          {t("remote.revokeDevice")}
        </button>
        <button type="button" onClick={() => void access.signOut()} disabled={busy}>
          {t("remote.signOut")}
        </button>
      </section>
      <section className="remote-pairing" aria-label={t("remote.createPairing")}>
        <label>
          {t("remote.pairingName")}
          <input value={access.pairingName} onChange={(event) => access.setPairingName(event.target.value)} />
        </label>
        <button type="button" onClick={() => void access.createPairing()} disabled={!access.pairingName.trim() || busy}>
          {t("remote.createPairing")}
        </button>
        {access.pairingCode ? <output className="remote-pairing-code">{access.pairingCode}</output> : null}
      </section>
      <div className="remote-notice" role="status">
        {access.notice}
      </div>
    </main>
  );
}
