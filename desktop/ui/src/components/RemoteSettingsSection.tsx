import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "../lib/i18n";
import type { SidecarClient } from "../lib/sidecar";
import type { RemoteDeviceStatus, RemoteProjectTarget } from "../types";

const STATUS_POLL_INTERVAL_MS = 3000;

type RemoteSettingsSectionProps = {
  client: SidecarClient;
  collectProjects: (() => Promise<RemoteProjectTarget[]>) | null;
};

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function RemoteSettingsSection({ client, collectProjects }: RemoteSettingsSectionProps) {
  const { t } = useI18n();
  const [status, setStatus] = useState<RemoteDeviceStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [relayUrl, setRelayUrl] = useState("");
  const prefilledRef = useRef(false);
  // The parent may hand us a fresh SidecarClient per render; keep effects
  // keyed to the stable base URL and always call the latest client.
  const clientRef = useRef(client);
  clientRef.current = client;
  const collectRef = useRef(collectProjects);
  collectRef.current = collectProjects;
  const baseUrl = client.baseUrl;

  const refresh = useCallback(async () => {
    try {
      const next = await clientRef.current.getRemoteStatus();
      setStatus(next);
      setError("");
    } catch (refreshError) {
      setError(formatError(refreshError));
    }
  }, [baseUrl]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, STATUS_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!status || prefilledRef.current) {
      return;
    }
    prefilledRef.current = true;
    setRelayUrl((current) => current || status.relay_url);
  }, [status]);

  async function runAction(action: () => Promise<RemoteDeviceStatus>) {
    setBusy(true);
    setError("");
    try {
      setStatus(await action());
    } catch (actionError) {
      setError(formatError(actionError));
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function handlePairAndEnable() {
    // Device flow: the sidecar creates a Relay pair session, opens the
    // approval page in the system browser, and finishes pairing on its own
    // once the user approves there — no credentials typed here.
    await runAction(() => client.pairBeginRemoteDevice(relayUrl.trim()));
  }

  async function handleCancelPair() {
    await runAction(() => client.pairCancelRemoteDevice());
  }

  async function handleToggle() {
    if (!status) {
      return;
    }
    await runAction(async () => {
      if (status.enabled) {
        return client.disableRemoteDevice();
      }
      // Enabling exposes every managed Desktop project through this sidecar's
      // embedded Connector, not just the active workspace.
      const projects = collectRef.current ? await collectRef.current() : [];
      return client.enableRemoteDevice(projects.length > 0 ? projects : undefined);
    });
  }

  async function handleUnpair() {
    if (!window.confirm(t("settings.remote.unpairConfirm"))) {
      return;
    }
    await runAction(() => client.unpairRemoteDevice());
  }

  const paired = Boolean(status?.paired);
  const pairPending = Boolean(status?.pair_pending);
  const online = Boolean(status?.connector_running);
  const pairFormReady = Boolean(relayUrl.trim());

  return (
    <div className="settings-group remote-settings-group">
      <p className="remote-settings-description">{t("settings.remote.description")}</p>
      {paired && status ? (
        <div className="remote-settings-panel">
          <dl className="remote-settings-facts">
            <div>
              <dt>{t("settings.remote.device")}</dt>
              <dd>{status.device_name || status.device_id}</dd>
            </div>
            <div>
              <dt>{t("settings.remote.relay")}</dt>
              <dd>{status.relay_url}</dd>
            </div>
            <div>
              <dt>{t("settings.remote.state")}</dt>
              <dd>
                <span className={`remote-settings-state ${online ? "online" : "offline"}`}>
                  {online ? t("settings.remote.online") : t("settings.remote.offline")}
                </span>
              </dd>
            </div>
            {status.projects.length > 0 ? (
              <div>
                <dt>{t("settings.remote.projects")}</dt>
                <dd>
                  <ul className="remote-settings-projects">
                    {status.projects.map((project) => (
                      <li key={project.project_id}>{project.name}</li>
                    ))}
                  </ul>
                </dd>
              </div>
            ) : null}
          </dl>
          {status.last_error ? <p className="remote-settings-error">{t("settings.remote.lastError", { error: status.last_error })}</p> : null}
          <div className="remote-settings-actions">
            <button className="settings-action-button" type="button" onClick={() => void handleToggle()} disabled={busy}>
              {busy
                ? t("settings.remote.working")
                : status.enabled
                  ? t("settings.remote.disable")
                  : t("settings.remote.enable")}
            </button>
            <button className="settings-action-button danger" type="button" onClick={() => void handleUnpair()} disabled={busy}>
              {t("settings.remote.unpair")}
            </button>
          </div>
        </div>
      ) : (
        <form
          className="remote-settings-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!busy && !pairPending && pairFormReady) {
              void handlePairAndEnable();
            }
          }}
        >
          <label>
            <span>{t("settings.remote.relayUrl")}</span>
            <input
              type="text"
              value={relayUrl}
              onChange={(event) => setRelayUrl(event.currentTarget.value)}
              placeholder="https://relay.example.com"
              autoComplete="off"
              disabled={pairPending}
            />
          </label>
          {pairPending ? (
            <>
              <p className="remote-settings-pending">{t("settings.remote.pairPending")}</p>
              <div className="remote-settings-actions">
                <button className="settings-action-button" type="button" onClick={() => void handleCancelPair()} disabled={busy}>
                  {busy ? t("settings.remote.working") : t("settings.remote.cancelPair")}
                </button>
              </div>
            </>
          ) : (
            <div className="remote-settings-actions">
              <button className="settings-action-button" type="submit" disabled={busy || !pairFormReady}>
                {busy ? t("settings.remote.working") : t("settings.remote.pairAndEnable")}
              </button>
            </div>
          )}
        </form>
      )}
      {error ? <p className="remote-settings-error" role="alert">{error}</p> : null}
    </div>
  );
}

export default RemoteSettingsSection;
