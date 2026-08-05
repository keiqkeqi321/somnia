import { afterEach, describe, expect, it, vi } from "vitest";

import { parseOAuthBindFragment, readOAuthError, stripOAuthError } from "./remote-oauth";
import { RemoteRelayClient } from "./remote-relay-client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("remote oauth helpers", () => {
  it("parses the bind callback fragment", () => {
    expect(parseOAuthBindFragment("#provider=github&code=abc&state=xyz")).toEqual({
      provider: "github",
      code: "abc",
      state: "xyz",
    });
  });

  it("rejects route hashes and incomplete fragments", () => {
    expect(parseOAuthBindFragment("#/connect")).toBeNull();
    expect(parseOAuthBindFragment("#/pair?session=s-1&secret=abc")).toBeNull();
    expect(parseOAuthBindFragment("")).toBeNull();
    // The fragment is only a bind grant when `provider` leads — a reordered
    // fragment is not produced by the Relay callback and is not recognized.
    expect(parseOAuthBindFragment("#state=xyz&code=abc&provider=github")).toBeNull();
    expect(parseOAuthBindFragment("#provider=github&code=abc")).toBeNull();
    expect(parseOAuthBindFragment("#provider=github&state=xyz")).toBeNull();
    expect(parseOAuthBindFragment("#code=abc&state=xyz")).toBeNull();
  });

  it("reads the oauth_error slug when present", () => {
    expect(readOAuthError("?oauth_error=github_login_failed")).toBe("github_login_failed");
    expect(readOAuthError("?remote=1&oauth_error=github_login_failed")).toBe("github_login_failed");
    expect(readOAuthError("?remote=1")).toBeNull();
    expect(readOAuthError("")).toBeNull();
  });

  it("strips oauth_error while keeping other query params and the hash", () => {
    vi.stubGlobal("window", { location: { pathname: "/app/" } });
    expect(stripOAuthError("?oauth_error=github_login_failed", "")).toBe("/app/");
    expect(stripOAuthError("?remote=1&oauth_error=github_login_failed", "#/login")).toBe("/app/?remote=1#/login");
    expect(stripOAuthError("", "#/connect")).toBe("/app/#/connect");
  });

  it("builds the authorize URL with mode and redirect query parameters", () => {
    const client = new RemoteRelayClient("https://relay.example.com");
    const url = new URL(client.oauthAuthorizeUrl("github", "login", "https://somnia.top/app/"));
    expect(url.origin).toBe("https://relay.example.com");
    expect(url.pathname).toBe("/api/auth/github/authorize");
    expect(url.searchParams.get("mode")).toBe("login");
    expect(url.searchParams.get("redirect")).toBe("https://somnia.top/app/");

    const bindUrl = new URL(client.oauthAuthorizeUrl("github", "bind", "https://somnia.top/app/"));
    expect(bindUrl.searchParams.get("mode")).toBe("bind");
  });
});
