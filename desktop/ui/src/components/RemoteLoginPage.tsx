import { useI18n } from "../lib/i18n";
import type { useRemoteAccess } from "../lib/use-remote-access";
import appIconUrl from "../../src-tauri/icons/32x32.png";

type RemoteAccess = ReturnType<typeof useRemoteAccess>;

type RemoteLoginPageProps = {
  access: RemoteAccess;
};

/**
 * Remote-mode `#/login` route: sign in against the relay. The relay sets a
 * cookie session, so a later refresh can restore access without this form.
 */
export default function RemoteLoginPage({ access }: RemoteLoginPageProps) {
  const { t } = useI18n();

  return (
    <main className="remote-shell remote-shell-login">
      <form
        className="remote-login"
        onSubmit={(event) => {
          event.preventDefault();
          void access.signIn();
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
            autoComplete="current-password"
          />
        </label>
        <button type="submit" disabled={access.busy || !access.username.trim() || !access.password}>
          {t("remote.signIn")}
        </button>
        <div className="remote-notice" role="status">
          {access.notice}
        </div>
      </form>
    </main>
  );
}
