import { describe, expect, it } from "vitest";

import { parseRemoteRoute, remoteRouteHash, resolveRemoteRoute } from "./remote-router";

describe("remote hash router", () => {
  it("parses known routes and rejects empty/unknown hashes", () => {
    expect(parseRemoteRoute("#/login")).toBe("login");
    expect(parseRemoteRoute("#/register")).toBe("register");
    expect(parseRemoteRoute("#/connect")).toBe("connect");
    expect(parseRemoteRoute("#/workspace")).toBe("workspace");
    expect(parseRemoteRoute("")).toBeNull();
    expect(parseRemoteRoute("#")).toBeNull();
    expect(parseRemoteRoute("#/")).toBeNull();
    expect(parseRemoteRoute("#/nope")).toBeNull();
    expect(parseRemoteRoute("#/login/extra")).toBeNull();
  });

  it("formats route hashes", () => {
    expect(remoteRouteHash("login")).toBe("#/login");
    expect(remoteRouteHash("register")).toBe("#/register");
    expect(remoteRouteHash("connect")).toBe("#/connect");
    expect(remoteRouteHash("workspace")).toBe("#/workspace");
  });

  it("resolves the route from auth/connection state", () => {
    expect(resolveRemoteRoute({ authenticated: false, connected: false })).toBe("login");
    expect(resolveRemoteRoute({ authenticated: true, connected: false })).toBe("connect");
    expect(resolveRemoteRoute({ authenticated: true, connected: true })).toBe("workspace");
    // A live connection implies an authenticated session.
    expect(resolveRemoteRoute({ authenticated: false, connected: true })).toBe("workspace");
  });

  it("keeps login and register legal while signed out, defaulting empty/unknown hashes to login", () => {
    expect(resolveRemoteRoute({ authenticated: false, connected: false, hash: "#/login" })).toBe("login");
    expect(resolveRemoteRoute({ authenticated: false, connected: false, hash: "#/register" })).toBe("register");
    expect(resolveRemoteRoute({ authenticated: false, connected: false, hash: "" })).toBe("login");
    expect(resolveRemoteRoute({ authenticated: false, connected: false, hash: "#/nope" })).toBe("login");
    // Signed-out users deep-linking into gated routes land on login.
    expect(resolveRemoteRoute({ authenticated: false, connected: false, hash: "#/connect" })).toBe("login");
    expect(resolveRemoteRoute({ authenticated: false, connected: false, hash: "#/workspace" })).toBe("login");
  });

  it("redirects authenticated users away from login/register", () => {
    expect(resolveRemoteRoute({ authenticated: true, connected: false, hash: "#/login" })).toBe("connect");
    expect(resolveRemoteRoute({ authenticated: true, connected: false, hash: "#/register" })).toBe("connect");
    expect(resolveRemoteRoute({ authenticated: true, connected: true, hash: "#/register" })).toBe("workspace");
  });
});
