import { invoke } from "@tauri-apps/api/core";

import type { ManagedSidecarConnection } from "../types";

type TauriWindow = Window & {
  __TAURI_INTERNALS__?: {
    invoke?: unknown;
  };
};

function isTauriEnvironment(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  const tauriWindow = window as TauriWindow;
  return typeof tauriWindow.__TAURI_INTERNALS__?.invoke === "function";
}

async function ensureManagedSidecar(): Promise<ManagedSidecarConnection | null> {
  if (!isTauriEnvironment()) {
    return null;
  }
  return invoke<ManagedSidecarConnection>("ensure_managed_sidecar");
}

export { ensureManagedSidecar, isTauriEnvironment };
