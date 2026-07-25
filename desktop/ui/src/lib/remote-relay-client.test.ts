import { describe, expect, it } from "vitest";

import { RemoteRelayClient } from "./remote-relay-client";

describe("RemoteRelayClient", () => {
  it("builds a copyable pairing link without embedding credentials", () => {
    const client = new RemoteRelayClient("https://somnia.top");
    const link = client.pairingLink({ code: "ABCD1234", expires_at: 123 });
    const parsed = new URL(link);

    expect(parsed.protocol).toBe("somnia:");
    expect(parsed.hostname).toBe("pair");
    expect(parsed.searchParams.get("relay")).toBe("https://somnia.top");
    expect(parsed.searchParams.get("code")).toBe("ABCD1234");
    expect(link).not.toContain("password");
  });
});
