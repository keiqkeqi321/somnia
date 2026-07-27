import { describe, expect, it } from "vitest";

import { parseRemoteRoute, remoteRouteHash, resolveRemoteRoute } from "./remote-router";

describe("remote hash router", () => {
  it("parses known routes and rejects empty/unknown hashes", () => {
    expect(parseRemoteRoute("#/login")).toBe("login");
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
});
