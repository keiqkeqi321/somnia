import { describe, expect, it } from "vitest";

import { parsePairLink, parseRemoteRoute, remoteRouteHash, resolveRemoteRoute } from "./remote-router";

describe("remote hash router", () => {
  it("parses known routes and rejects empty/unknown hashes", () => {
    expect(parseRemoteRoute("#/login")).toBe("login");
    expect(parseRemoteRoute("#/register")).toBe("register");
    expect(parseRemoteRoute("#/connect")).toBe("connect");
    expect(parseRemoteRoute("#/workspace")).toBe("workspace");
    expect(parseRemoteRoute("#/pair")).toBe("pair");
    expect(parseRemoteRoute("")).toBeNull();
    expect(parseRemoteRoute("#")).toBeNull();
    expect(parseRemoteRoute("#/")).toBeNull();
    expect(parseRemoteRoute("#/nope")).toBeNull();
    expect(parseRemoteRoute("#/login/extra")).toBeNull();
  });

  it("parses the pair deep link with its in-hash query", () => {
    expect(parseRemoteRoute("#/pair?session=s-1&secret=abc")).toBe("pair");
    expect(parsePairLink("#/pair?session=s-1&secret=abc")).toEqual({ sessionId: "s-1", secret: "abc" });
    expect(parsePairLink("#/pair?secret=abc&session=s-1")).toEqual({ sessionId: "s-1", secret: "abc" });
    expect(parsePairLink("#/pair")).toBeNull();
    expect(parsePairLink("#/pair?session=s-1")).toBeNull();
    expect(parsePairLink("#/pair?secret=abc")).toBeNull();
    expect(parsePairLink("#/connect")).toBeNull();
  });

  it("formats route hashes", () => {
    expect(remoteRouteHash("login")).toBe("#/login");
    expect(remoteRouteHash("register")).toBe("#/register");
    expect(remoteRouteHash("connect")).toBe("#/connect");
    expect(remoteRouteHash("workspace")).toBe("#/workspace");
    expect(remoteRouteHash("pair")).toBe("#/pair");
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

  it("keeps the pair deep link legal while signed out and after sign-in", () => {
    expect(resolveRemoteRoute({ authenticated: false, connected: false, hash: "#/pair?session=s-1&secret=abc" })).toBe("pair");
    expect(resolveRemoteRoute({ authenticated: true, connected: false, hash: "#/pair?session=s-1&secret=abc" })).toBe("pair");
    // An active workspace connection still wins over the deep link.
    expect(resolveRemoteRoute({ authenticated: true, connected: true, hash: "#/pair?session=s-1&secret=abc" })).toBe("workspace");
  });
});
