/**
 * OAuth helpers for the Remote-mode provider sign-in/bind round trips (e.g.
 * GitHub). The SPA navigates away to the Relay authorize endpoint and lands
 * back on the bare SPA address — these helpers parse what the callback left
 * in the URL. Location pieces are passed in so the functions stay pure and
 * trivially testable.
 */

/** The bare SPA address used as the OAuth `redirect` — no query, no hash. */
export function spaSelfUrl(): string {
  return window.location.origin + window.location.pathname;
}

/** Display names for the OAuth channels a Relay may report via `/api/info`. */
export const OAUTH_PROVIDER_LABELS: Record<string, string> = {
  github: "GitHub",
  gitee: "Gitee",
};

/** Human-readable channel name; falls back to the raw provider id for unknown channels. */
export function oauthProviderLabel(provider: string): string {
  return OAUTH_PROVIDER_LABELS[provider] ?? provider;
}

/**
 * Parses the bind-mode callback fragment (`#provider=github&code=…&state=…`).
 * This fragment is not a router hash — returns `null` for route hashes
 * (`#/connect`) or when any parameter is missing.
 */
export function parseOAuthBindFragment(hash: string): { provider: string; code: string; state: string } | null {
  if (!hash.startsWith("#provider=")) {
    return null;
  }
  const params = new URLSearchParams(hash.slice(1));
  const provider = params.get("provider") ?? "";
  const code = params.get("code") ?? "";
  const state = params.get("state") ?? "";
  if (!provider || !code || !state) {
    return null;
  }
  return { provider, code, state };
}

/** Reads the `oauth_error` slug the login-mode callback appends on failure. */
export function readOAuthError(search: string): string | null {
  return new URLSearchParams(search).get("oauth_error");
}

/**
 * Strips `oauth_error` from the query, returning the current page as a
 * relative URL suitable for `history.replaceState`. Other query parameters
 * and the hash are preserved.
 */
export function stripOAuthError(search: string, hash: string): string {
  const params = new URLSearchParams(search);
  params.delete("oauth_error");
  const query = params.toString();
  return `${window.location.pathname}${query ? `?${query}` : ""}${hash}`;
}
