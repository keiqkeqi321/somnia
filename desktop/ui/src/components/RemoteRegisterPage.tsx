import { useEffect, useState } from "react";

import { useI18n } from "../lib/i18n";
import type { useRemoteAccess } from "../lib/use-remote-access";
import appIconUrl from "../../src-tauri/icons/32x32.png";

type RemoteAccess = ReturnType<typeof useRemoteAccess>;

type RemoteRegisterPageProps = {
  access: RemoteAccess;
};

const MIN_PASSWORD_LENGTH = 8;

/**
 * Remote-mode `#/register` route: self-service account creation. The Relay
 * issues a cookie session on success, so `signUp` lands authenticated and
 * the router moves on to `#/connect` by itself.
 */
export default function RemoteRegisterPage({ access }: RemoteRegisterPageProps) {
  const { t } = useI18n();
  const [confirmPassword, setConfirmPassword] = useState("");

  useEffect(() => {
    access.setNotice(t("remote.registerNotice"));
    // Intentionally run only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleSubmit() {
    if (access.password.length < MIN_PASSWORD_LENGTH) {
      access.setNotice(t("remote.passwordTooShort"));
      return;
    }
    if (access.password !== confirmPassword) {
      access.setNotice(t("remote.passwordMismatch"));
      return;
    }
    void access.signUp();
  }

  return (
    <main className="remote-shell remote-shell-login">
      <form
        className="remote-login"
        onSubmit={(event) => {
          event.preventDefault();
          handleSubmit();
        }}
      >
        <div className="remote-brand">
          <img className="remote-brand-icon" src={appIconUrl} alt="" aria-hidden="true" />
          <h1>{t("remote.title")}</h1>
        </div>
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
            autoComplete="new-password"
          />
        </label>
        <label>
          {t("remote.confirmPassword")}
          <input
            type="password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            autoComplete="new-password"
          />
        </label>
        <button type="submit" disabled={access.busy || !access.username.trim() || !access.password || !confirmPassword}>
          {t("remote.signUp")}
        </button>
        <a className="remote-login-link" href="#/login">
          {t("remote.haveAccountSignIn")}
        </a>
        <div className="remote-notice" role="status">
          {access.notice}
        </div>
      </form>
    </main>
  );
}
