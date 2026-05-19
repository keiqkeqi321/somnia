import { invoke } from "@tauri-apps/api/core";
import type { UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";

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

async function ensureManagedSidecar(workspacePath?: string): Promise<ManagedSidecarConnection | null> {
  if (!isTauriEnvironment()) {
    return null;
  }
  return invoke<ManagedSidecarConnection>("ensure_managed_sidecar", { workspacePath: workspacePath ?? null });
}

async function chooseProjectFolder(): Promise<string | null> {
  if (!isTauriEnvironment()) {
    return null;
  }
  return invoke<string | null>("choose_project_folder");
}

async function stopManagedSidecar(workspacePath?: string): Promise<boolean> {
  if (!isTauriEnvironment()) {
    return false;
  }
  return invoke<boolean>("stop_managed_sidecar", { workspacePath: workspacePath ?? null });
}

async function openWorkspaceRoot(path: string): Promise<void> {
  if (!isTauriEnvironment()) {
    return;
  }
  await invoke("open_workspace_root", { path });
}

async function minimizeMainWindow(): Promise<void> {
  if (!isTauriEnvironment()) {
    return;
  }
  await getCurrentWindow().minimize();
}

async function toggleMaximizeMainWindow(): Promise<void> {
  if (!isTauriEnvironment()) {
    return;
  }
  await getCurrentWindow().toggleMaximize();
}

async function isMainWindowMaximized(): Promise<boolean> {
  if (!isTauriEnvironment()) {
    return false;
  }
  return getCurrentWindow().isMaximized();
}

async function onMainWindowResized(callback: () => void): Promise<UnlistenFn | null> {
  if (!isTauriEnvironment()) {
    return null;
  }
  return getCurrentWindow().onResized(callback);
}

async function closeMainWindow(): Promise<void> {
  if (!isTauriEnvironment()) {
    return;
  }
  await stopManagedSidecar();
  await getCurrentWindow().close();
}

async function startMainWindowDrag(): Promise<void> {
  if (!isTauriEnvironment()) {
    return;
  }
  await getCurrentWindow().startDragging();
}

export {
  closeMainWindow,
  chooseProjectFolder,
  ensureManagedSidecar,
  isMainWindowMaximized,
  isTauriEnvironment,
  minimizeMainWindow,
  onMainWindowResized,
  openWorkspaceRoot,
  startMainWindowDrag,
  stopManagedSidecar,
  toggleMaximizeMainWindow,
};
