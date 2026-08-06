import { useI18n } from "../lib/i18n";
import type { useRemoteAccess } from "../lib/use-remote-access";
import appIconUrl from "../../src-tauri/icons/32x32.png";

type RemoteAccess = ReturnType<typeof useRemoteAccess>;

type RemoteLoginPageProps = {
  access: RemoteAccess;
};

/**
 * Remote-mode `#/login` route: channel sign-in only (GitHub OAuth via the
 * relay). Username/password sign-in is intentionally hidden from the UI for
 * maintainability — the relay still serves the password endpoints for API
 * clients and administrative deep links.
 */
export default function RemoteLoginPage({ access }: RemoteLoginPageProps) {
  const { t } = useI18n();

  return (
    <main className="remote-shell remote-shell-login">
      <div className="remote-login">
        <div className="remote-brand">
          <img className="remote-brand-icon" src={appIconUrl} alt="" aria-hidden="true" />
          <h1>{t("remote.title")}</h1>
        </div>
        <button type="button" onClick={() => access.beginGitHubSignIn()} disabled={access.busy}>
          {t("remote.continueWithGitHub")}
        </button>
        <div className="remote-notice" role="status">
          {access.notice}
        </div>
      </div>
    </main>
  );
}
