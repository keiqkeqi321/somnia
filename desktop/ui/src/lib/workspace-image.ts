import { useEffect, useState } from "react";

import type { SomniaClient } from "./somnia-client";

export interface WorkspaceImageReference {
  path?: string;
  absolute_path?: string;
  image_url?: string;
}

// Resolved image sources are cached per client and workspace path so
// re-rendering a conversation does not re-fetch data URLs over the relay.
const resolvedSources = new WeakMap<SomniaClient, Map<string, string>>();
const pendingSources = new WeakMap<SomniaClient, Map<string, Promise<string>>>();

export function workspaceImagePath(image: WorkspaceImageReference): string {
  return String(image.path || image.absolute_path || "").trim();
}

/**
 * Synchronous fast path for image rendering: direct http(s)/data URLs render
 * as-is, and clients with an HTTP base URL (Direct/desktop) keep the plain
 * `/workspace/images` link. Returns `null` when the image must be resolved
 * asynchronously through `client.getWorkspaceImage` (Remote mode), or an
 * empty string when the image cannot be rendered at all.
 */
export function immediateWorkspaceImageSource(image: WorkspaceImageReference, client: SomniaClient | null): string | null {
  const imageUrl = String(image.image_url ?? "").trim();
  if (/^(?:https?:|data:image\/)/i.test(imageUrl)) {
    return imageUrl;
  }
  const path = workspaceImagePath(image);
  if (!path || !client) {
    return "";
  }
  const baseUrl = String(client.baseUrl ?? "").trim().replace(/\/+$/, "");
  if (!baseUrl) {
    return null;
  }
  return `${baseUrl}/workspace/images?path=${encodeURIComponent(path)}`;
}

/**
 * Asynchronously resolves a workspace image through the client, deduplicating
 * in-flight requests and caching successful results per client + path.
 */
export function loadWorkspaceImageSource(client: SomniaClient, path: string): Promise<string> {
  const cached = resolvedSources.get(client)?.get(path);
  if (cached) {
    return Promise.resolve(cached);
  }
  let pending = pendingSources.get(client);
  if (!pending) {
    pending = new Map();
    pendingSources.set(client, pending);
  }
  const inFlight = pending.get(path);
  if (inFlight) {
    return inFlight;
  }
  const request = client
    .getWorkspaceImage(path)
    .then((src) => {
      let cache = resolvedSources.get(client);
      if (!cache) {
        cache = new Map();
        resolvedSources.set(client, cache);
      }
      cache.set(path, src);
      pending.delete(path);
      return src;
    })
    .catch((error: unknown) => {
      pending.delete(path);
      throw error;
    });
  pending.set(path, request);
  return request;
}

/**
 * Resolves the renderable `src` for a workspace image reference. Desktop
 * (Direct) clients resolve synchronously via the base URL fast path; Remote
 * clients fetch an authenticated data URL. Resolution is cancelled when the
 * component unmounts or the inputs change.
 */
export function useWorkspaceImageSource(image: WorkspaceImageReference, client: SomniaClient | null): string {
  const immediate = immediateWorkspaceImageSource(image, client);
  const [src, setSrc] = useState<string>(immediate ?? "");
  useEffect(() => {
    if (immediate !== null) {
      setSrc(immediate);
      return;
    }
    const path = workspaceImagePath(image);
    if (!client || !path) {
      setSrc("");
      return;
    }
    let cancelled = false;
    loadWorkspaceImageSource(client, path)
      .then((resolved) => {
        if (!cancelled) {
          setSrc(resolved);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSrc("");
        }
      });
    return () => {
      cancelled = true;
    };
    // `immediate` is derived from the remaining dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, immediate, image.path, image.absolute_path, image.image_url]);
  return src;
}
