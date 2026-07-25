import { describe, expect, it } from "vitest";

import { deriveRemoteConnectionState, remoteConnectionCopy } from "./remote-connection-state";

describe("remote connection state", () => {
  it("distinguishes online device from a ready project", () => {
    expect(deriveRemoteConnectionState({ transport: "disconnected", deviceStatus: "online", hasProject: true })).toBe("offline");
    expect(deriveRemoteConnectionState({ transport: "disconnected", deviceStatus: "reconnecting", hasProject: true })).toBe("reconnecting");
    expect(deriveRemoteConnectionState({ transport: "connected", deviceStatus: "online", hasProject: true })).toBe("ready");
  });

  it("prioritizes local confirmation and resynchronization", () => {
    expect(deriveRemoteConnectionState({ transport: "connected", deviceStatus: "online", hasProject: true, waitingForLocalConfirmation: true })).toBe("local-confirmation");
    expect(deriveRemoteConnectionState({ transport: "connected", deviceStatus: "online", hasProject: true, resynchronizing: true })).toBe("resynchronizing");
  });

  it("provides an action with each state", () => {
    expect(remoteConnectionCopy("offline")).toEqual({ label: "Computer offline; draft kept", action: "Retry connection" });
  });
});
