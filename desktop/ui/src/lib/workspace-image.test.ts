import { describe, expect, it } from "vitest";

import type { SomniaClient } from "./somnia-client";
import { immediateWorkspaceImageSource, loadWorkspaceImageSource } from "./workspace-image";

function fakeClient(options: { baseUrl: string; resolve?: (path: string) => Promise<string> }): SomniaClient & { calls: string[] } {
  const calls: string[] = [];
  const client = {
    baseUrl: options.baseUrl,
    calls,
    getWorkspaceImage(path: string): Promise<string> {
      calls.push(path);
      return options.resolve ? options.resolve(path) : Promise.resolve(`data:image/png;base64,${path}`);
    },
  };
  return client as unknown as SomniaClient & { calls: string[] };
}

describe("immediateWorkspaceImageSource", () => {
  it("passes through http(s) and data URLs unchanged", () => {
    expect(immediateWorkspaceImageSource({ image_url: "https://example.com/a.png" }, null)).toBe("https://example.com/a.png");
    expect(immediateWorkspaceImageSource({ image_url: "data:image/png;base64,AAA" }, null)).toBe("data:image/png;base64,AAA");
  });

  it("keeps the synchronous /workspace/images URL fast path for Direct clients", () => {
    const client = fakeClient({ baseUrl: "http://127.0.0.1:8765/" });
    expect(immediateWorkspaceImageSource({ path: "shots/a b.png" }, client)).toBe(
      "http://127.0.0.1:8765/workspace/images?path=shots%2Fa%20b.png",
    );
    expect(immediateWorkspaceImageSource({ absolute_path: "C:/workspace/a.png" }, client)).toBe(
      "http://127.0.0.1:8765/workspace/images?path=C%3A%2Fworkspace%2Fa.png",
    );
  });

  it("returns null for base-URL-less (Remote) clients so they resolve asynchronously", () => {
    const client = fakeClient({ baseUrl: "" });
    expect(immediateWorkspaceImageSource({ path: "shots/a.png" }, client)).toBeNull();
  });

  it("returns an empty string when the image cannot be rendered", () => {
    expect(immediateWorkspaceImageSource({}, fakeClient({ baseUrl: "http://x" }))).toBe("");
    expect(immediateWorkspaceImageSource({ path: "a.png" }, null)).toBe("");
  });
});

describe("loadWorkspaceImageSource", () => {
  it("deduplicates in-flight requests and caches resolved sources per client and path", async () => {
    const client = fakeClient({ baseUrl: "" });
    const [first, second] = await Promise.all([
      loadWorkspaceImageSource(client, "shots/a.png"),
      loadWorkspaceImageSource(client, "shots/a.png"),
    ]);
    expect(first).toBe("data:image/png;base64,shots/a.png");
    expect(second).toBe(first);
    await loadWorkspaceImageSource(client, "shots/a.png");
    expect(client.calls).toEqual(["shots/a.png"]);
  });

  it("caches per client instance", async () => {
    const first = fakeClient({ baseUrl: "" });
    const second = fakeClient({ baseUrl: "" });
    await loadWorkspaceImageSource(first, "shots/a.png");
    await loadWorkspaceImageSource(second, "shots/a.png");
    expect(first.calls).toEqual(["shots/a.png"]);
    expect(second.calls).toEqual(["shots/a.png"]);
  });

  it("does not cache failures so a later retry can succeed", async () => {
    let attempts = 0;
    const client = fakeClient({
      baseUrl: "",
      resolve: (path) => {
        attempts += 1;
        return attempts === 1 ? Promise.reject(new Error("relay offline")) : Promise.resolve(`data:image/png;base64,${path}`);
      },
    });
    await expect(loadWorkspaceImageSource(client, "shots/a.png")).rejects.toThrow("relay offline");
    await expect(loadWorkspaceImageSource(client, "shots/a.png")).resolves.toBe("data:image/png;base64,shots/a.png");
    expect(client.calls).toEqual(["shots/a.png", "shots/a.png"]);
  });
});
