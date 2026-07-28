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
 * - `#/pair`      — device-flow approval (deep link `#/pair?session=<id>&secret=<s>`
 *   opened by Desktop; legal while signed out — the sign-in form shows in place
 *   and the same hash resolves back here after sign-in)
 */

export type RemoteRouteName = "login" | "register" | "connect" | "workspace" | "pair";

/** Parses a location hash into a known route, or `null` for empty/unknown hashes. */
export function parseRemoteRoute(hash: string): RemoteRouteName | null {
  // The pair deep link carries its parameters as a query inside the hash
  // (`#/pair?session=…&secret=…`); routing only looks at the path part.
  switch (hash.replace(/^#/, "").split("?")[0]) {
    case "/login":
      return "login";
    case "/register":
      return "register";
    case "/connect":
      return "connect";
    case "/workspace":
      return "workspace";
    case "/pair":
      return "pair";
    default:
      return null;
  }
}

/**
 * Extracts the device-flow parameters from a `#/pair?session=…&secret=…`
 * hash. Returns `null` for other routes or when either parameter is missing.
 */
export function parsePairLink(hash: string): { sessionId: string; secret: string } | null {
  if (parseRemoteRoute(hash) !== "pair") {
    return null;
  }
  const query = hash.replace(/^#/, "").split("?")[1] ?? "";
  const params = new URLSearchParams(query);
  const sessionId = params.get("session") ?? "";
  const secret = params.get("secret") ?? "";
  if (!sessionId || !secret) {
    return null;
  }
  return { sessionId, secret };
}

/**
 * Derives the valid route for the current auth/connection state. Empty or
 * unknown hashes resolve through this as well, so the URL always converges
 * to a route matching reality. While signed out both `#/login` and
 * `#/register` are legal, so the current `hash` is honored when it names
 * either of them; signed-in users hitting either route land on `#/connect`.
 * A `#/pair` deep link is honored in every state except an active workspace
 * connection, so the approval page survives the sign-in round trip.
 */
export function resolveRemoteRoute(state: { authenticated: boolean; connected: boolean; hash?: string }): RemoteRouteName {
  if (state.connected) {
    return "workspace";
  }
  const requested = state.hash === undefined ? null : parseRemoteRoute(state.hash);
  if (requested === "pair") {
    return "pair";
  }
  if (state.authenticated) {
    return "connect";
  }
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
