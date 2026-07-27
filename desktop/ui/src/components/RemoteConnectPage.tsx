import { useEffect, useState } from "react";

import { useI18n } from "../lib/i18n";
import type { useRemoteAccess } from "../lib/use-remote-access";
import appIconUrl from "../../src-tauri/icons/32x32.png";

type RemoteAccess = ReturnType<typeof useRemoteAccess>;

type RemoteConnectPageProps = {
  access: RemoteAccess;
  connecting: boolean;
  onConnect: (deviceId: string, projectId: string) => void;
  onSignOut: () => void;
};

/**
 * Remote-mode `#/connect` route: pick a paired Device, then one of the
 * Device's registered projects. Remote mode can only switch among
 * pre-registered projects — there is intentionally no project create/remove
 * UI here. Pairing, revoke, and sign-out also live on this page.
 */
export default function RemoteConnectPage({ access, connecting, onConnect, onSignOut }: RemoteConnectPageProps) {
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

  return (
    <main className="remote-shell remote-shell-gate">
      <section className="remote-connection" aria-label={t("remote.title")}>
        <div className="remote-brand">
          <img className="remote-brand-icon" src={appIconUrl} alt="" aria-hidden="true" />
          <h1 className="remote-gate-title">{t("remote.title")}</h1>
        </div>
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
        <button type="button" onClick={onSignOut} disabled={busy}>
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
