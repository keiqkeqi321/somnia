import { useEffect, useState } from "react";

/**
 * Minimal hash router for Remote mode (`?remote=1`). Desktop mode never
 * touches the hash — these helpers are only wired up when remote mode is on.
 *
 * Routes:
 * - `#/login`     — relay sign-in (redirects to `#/connect` when authenticated)
 * - `#/register`  — self-service account creation (also legal only while signed out)
 * - `#/connect`   — device/project picker (redirects to `#/login` when signed out)
 * - `#/workspace` — connected workspace (redirects to `#/connect` when not connected)
 */

export type RemoteRouteName = "login" | "register" | "connect" | "workspace";

/** Parses a location hash into a known route, or `null` for empty/unknown hashes. */
export function parseRemoteRoute(hash: string): RemoteRouteName | null {
  switch (hash.replace(/^#/, "")) {
    case "/login":
      return "login";
    case "/register":
      return "register";
    case "/connect":
      return "connect";
    case "/workspace":
      return "workspace";
    default:
      return null;
  }
}

/**
 * Derives the valid route for the current auth/connection state. Empty or
 * unknown hashes resolve through this as well, so the URL always converges
 * to a route matching reality. While signed out both `#/login` and
 * `#/register` are legal, so the current `hash` is honored when it names
 * either of them; signed-in users hitting either route land on `#/connect`.
 */
export function resolveRemoteRoute(state: { authenticated: boolean; connected: boolean; hash?: string }): RemoteRouteName {
  if (state.connected) {
    return "workspace";
  }
  if (state.authenticated) {
    return "connect";
  }
  const requested = state.hash === undefined ? null : parseRemoteRoute(state.hash);
  if (requested === "login" || requested === "register") {
    return requested;
  }
  return "login";
}

export function remoteRouteHash(name: RemoteRouteName): string {
  return `#/${name}`;
}

/**
 * Navigates to a route. `replace` swaps the current history entry (used for
 * state-driven redirects); plain navigation pushes a new entry so deep links
 * stay shareable.
 */
export function navigateRemoteRoute(name: RemoteRouteName, options?: { replace?: boolean }): void {
  if (typeof window === "undefined") {
    return;
  }
  const hash = remoteRouteHash(name);
  if (window.location.hash === hash) {
    return;
  }
  if (options?.replace) {
    window.location.replace(`${window.location.pathname}${window.location.search}${hash}`);
    return;
  }
  window.location.hash = hash;
}

/** Tracks `window.location.hash`, updating on every `hashchange` event. */
export function useRemoteRouteHash(): string {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onHashChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return hash;
}
