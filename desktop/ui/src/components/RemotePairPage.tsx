import { useEffect, useState } from "react";

import { useI18n } from "../lib/i18n";
import { RemoteRelayClient, RemoteRelayError } from "../lib/remote-relay-client";
import { parsePairLink, useRemoteRouteHash } from "../lib/remote-router";
import appIconUrl from "../../src-tauri/icons/32x32.png";

type RemotePairPageProps = {
  relayUrl: string;
};

type PairPhase = "checking" | "ready" | "approving" | "done" | "invalid";

const DEFAULT_DEVICE_NAME = "My Computer";

/**
 * Remote-mode `#/pair?session=<id>&secret=<s>` route: the browser half of the
 * Desktop device-flow pairing. Desktop opens this deep link after creating a
 * pair session on the Relay; the signed-in user confirms a device name and
 * approves, which binds the pairing to their account. Signed-out users see
 * the regular sign-in form in place of this page — the hash never changes,
 * so the router lands back here once authenticated.
 */
export default function RemotePairPage({ relayUrl }: RemotePairPageProps) {
  const { t } = useI18n();
  const hash = useRemoteRouteHash();
  const link = parsePairLink(hash);
  const linkSessionId = link?.sessionId ?? null;
  const linkSecret = link?.secret ?? null;
  const [phase, setPhase] = useState<PairPhase>("checking");
  const [deviceName, setDeviceName] = useState(DEFAULT_DEVICE_NAME);
  const [notice, setNotice] = useState("");

  // Validate the session up front so expired/unknown links fail before the
  // user types anything.
  useEffect(() => {
    if (!linkSessionId || !linkSecret) {
      setPhase("invalid");
      return;
    }
    let cancelled = false;
    const client = new RemoteRelayClient(relayUrl);
    client
      .getPairSession(linkSessionId, linkSecret)
      .then((info) => {
        if (!cancelled) {
          const suggested = info.suggested_name?.trim();
          if (suggested) {
            setDeviceName(suggested);
          }
          setPhase(info.status === "pending" ? "ready" : "invalid");
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPhase("invalid");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [linkSessionId, linkSecret, relayUrl]);

  async function handleApprove() {
    if (!linkSessionId || !linkSecret || !deviceName.trim()) {
      return;
    }
    setPhase("approving");
    setNotice("");
    try {
      const client = new RemoteRelayClient(relayUrl);
      await client.approvePairSession(linkSessionId, linkSecret, deviceName.trim());
      setPhase("done");
    } catch (error) {
      // 401: cookie session lapsed — ask for a fresh sign-in. 403/4xx: the
      // session went stale between the check and the approval.
      if (error instanceof RemoteRelayError && error.status === 401) {
        setPhase("ready");
        setNotice(t("remote.pair.signInAgain"));
      } else if (error instanceof RemoteRelayError && error.status >= 400 && error.status < 500) {
        setPhase("invalid");
      } else {
        setPhase("ready");
        setNotice(error instanceof Error ? error.message : String(error));
      }
    }
  }

  return (
    <main className="remote-shell remote-shell-login">
      <form
        className="remote-login remote-pair"
        onSubmit={(event) => {
          event.preventDefault();
          if (phase === "ready") {
            void handleApprove();
          }
        }}
      >
        <div className="remote-brand">
          <img className="remote-brand-icon" src={appIconUrl} alt="" aria-hidden="true" />
          <h1>{t("remote.pair.title")}</h1>
        </div>
        {phase === "checking" ? (
          <div className="remote-notice" role="status">
            {t("remote.pair.checking")}
          </div>
        ) : null}
        {phase === "invalid" ? <p className="remote-pair-error">{t("remote.pair.invalid")}</p> : null}
        {phase === "ready" || phase === "approving" ? (
          <>
            <p className="remote-pair-description">{t("remote.pair.description")}</p>
            <label>
              {t("remote.pair.deviceName")}
              <input
                value={deviceName}
                onChange={(event) => setDeviceName(event.target.value)}
                autoComplete="off"
                disabled={phase === "approving"}
              />
            </label>
            <button type="submit" disabled={phase === "approving" || !deviceName.trim()}>
              {phase === "approving" ? t("remote.pair.approving") : t("remote.pair.approve")}
            </button>
          </>
        ) : null}
        {phase === "done" ? (
          <>
            <p className="remote-pair-done">{t("remote.pair.done")}</p>
            <button type="button" onClick={() => { window.location.hash = "#/connect"; }}>
              {t("remote.pair.gotoWorkspace")}
            </button>
          </>
        ) : null}
        {notice ? (
          <div className="remote-notice" role="alert">
            {notice}
          </div>
        ) : null}
      </form>
    </main>
  );
}
