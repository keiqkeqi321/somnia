import { describe, expect, it } from "vitest";

import { isRemoteProjectPath, parseRemoteProjectBucket, remoteProjectPath, remoteScopedStorageKey } from "./remote-storage";

describe("remote storage namespacing", () => {
  it("round-trips remote project paths", () => {
    const path = remoteProjectPath("device-1", "project-1");
    expect(path).toBe("remote://device-1/project-1");
    expect(isRemoteProjectPath(path)).toBe(true);
    expect(isRemoteProjectPath("C:/workspace/project")).toBe(false);
  });

  it("parses the device/project bucket, allowing slashes in the project id", () => {
    expect(parseRemoteProjectBucket("remote://device-1/project-1")).toEqual({ deviceId: "device-1", projectId: "project-1" });
    expect(parseRemoteProjectBucket("remote://device-1/group/project-1")).toEqual({ deviceId: "device-1", projectId: "group/project-1" });
  });

  it("rejects non-remote and malformed paths", () => {
    expect(parseRemoteProjectBucket("C:/workspace/project")).toBeNull();
    expect(parseRemoteProjectBucket(null)).toBeNull();
    expect(parseRemoteProjectBucket("remote://")).toBeNull();
    expect(parseRemoteProjectBucket("remote://device-1")).toBeNull();
    expect(parseRemoteProjectBucket("remote://device-1/")).toBeNull();
    expect(parseRemoteProjectBucket("remote:///project-1")).toBeNull();
  });

  it("buckets storage keys per device/project and leaves desktop paths unscoped", () => {
    expect(remoteScopedStorageKey("prompt-history", "remote://device-1/project-1")).toBe(
      "somnia.remote.prompt-history:device-1:project-1",
    );
    expect(remoteScopedStorageKey("last-opened-session", "remote://device-1/project-1")).toBe(
      "somnia.remote.last-opened-session:device-1:project-1",
    );
    expect(remoteScopedStorageKey("prompt-history", "remote://device-2/project-1")).toBe(
      "somnia.remote.prompt-history:device-2:project-1",
    );
    expect(remoteScopedStorageKey("prompt-history", "C:/workspace/project")).toBeNull();
  });
});
