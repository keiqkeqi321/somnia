import { useI18n, type TranslationKey } from "../lib/i18n";
import { oauthProviderLabel } from "../lib/remote-oauth";
import type { useRemoteAccess } from "../lib/use-remote-access";
import appIconUrl from "../../src-tauri/icons/32x32.png";

type RemoteAccess = ReturnType<typeof useRemoteAccess>;

type RemoteLoginPageProps = {
  access: RemoteAccess;
};

/** Per-channel sign-in button copy; unknown channels use the generic key. */
const PROVIDER_SIGN_IN_KEYS: Record<string, TranslationKey> = {
  github: "remote.continueWithGitHub",
  gitee: "remote.continueWithGitee",
};

/**
 * Remote-mode `#/login` route: channel sign-in only (OAuth via the relay).
 * Username/password sign-in is intentionally hidden from the UI for
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
        {access.oauthProviders.length > 0 ? (
          access.oauthProviders.map((provider) => (
            <button key={provider} type="button" onClick={() => access.beginOAuthSignIn(provider)} disabled={access.busy}>
              {t(PROVIDER_SIGN_IN_KEYS[provider] ?? "remote.continueWithProvider", { provider: oauthProviderLabel(provider) })}
            </button>
          ))
        ) : (
          <p className="remote-empty">{t("remote.noOAuthProviders")}</p>
        )}
        <div className="remote-notice" role="status">
          {access.notice}
        </div>
      </div>
    </main>
  );
}
