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
 * UI here. The "+" button above the Device picker opens the pairing dialog;
 * revoke and sign-out also live on this page.
 */
export default function RemoteConnectPage({ access, connecting, onConnect, onSignOut }: RemoteConnectPageProps) {
  const { t } = useI18n();
  const [projectId, setProjectId] = useState("");
  const [pairingOpen, setPairingOpen] = useState(false);

  const busy = access.busy || connecting;
  const selectedDevice = access.devices.find((device) => device.device_id === access.deviceId);
  const projects = selectedDevice?.projects ?? [];
  const githubIdentity = access.identities.find((identity) => identity.provider === "github");

  useEffect(() => {
    if (projects.length > 0 && !projects.some((project) => project.project_id === projectId)) {
      setProjectId(projects[0].project_id);
    } else if (projects.length === 0 && projectId) {
      setProjectId("");
    }
  }, [projectId, projects]);

  return (
    <main className="remote-shell remote-shell-gate">
      <div className="remote-gate-body">
        <section className="remote-connection" aria-label={t("remote.title")}>
          <div className="remote-brand">
            <img className="remote-brand-icon" src={appIconUrl} alt="" aria-hidden="true" />
            <h1 className="remote-gate-title">{t("remote.title")}</h1>
          </div>
          <div className="remote-field">
            <div className="remote-field-heading">
              <span>{t("remote.device")}</span>
              <button
                type="button"
                className="remote-add-device"
                aria-label={t("remote.addDevice")}
                title={t("remote.addDevice")}
                onClick={() => setPairingOpen(true)}
                disabled={busy}
              >
                +
              </button>
            </div>
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
          </div>
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
          {githubIdentity ? (
            <p className="remote-empty">GitHub: @{githubIdentity.provider_username}</p>
          ) : (
            <button type="button" onClick={() => access.beginGitHubBind()} disabled={busy}>
              {t("remote.bindGitHub")}
            </button>
          )}
        </section>
        <div className="remote-notice" role="status">
          {access.notice}
        </div>
      </div>
      {pairingOpen ? (
        <div className="remote-modal-backdrop" onClick={() => setPairingOpen(false)}>
          <div
            className="remote-modal"
            role="dialog"
            aria-modal="true"
            aria-label={t("remote.addDevice")}
            onClick={(event) => event.stopPropagation()}
          >
            <h2>{t("remote.addDevice")}</h2>
            <label>
              {t("remote.pairingName")}
              <input
                value={access.pairingName}
                onChange={(event) => access.setPairingName(event.target.value)}
                autoFocus
              />
            </label>
            <button
              type="button"
              onClick={() => void access.createPairing()}
              disabled={!access.pairingName.trim() || busy}
            >
              {t("remote.createPairing")}
            </button>
            {access.pairingCode ? <output className="remote-pairing-code">{access.pairingCode}</output> : null}
            <button type="button" className="remote-modal-close" onClick={() => setPairingOpen(false)}>
              {t("remote.close")}
            </button>
          </div>
        </div>
      ) : null}
    </main>
  );
}
