import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { I18nProvider } from "./lib/i18n";
import "./styles.css";

// Hosted Web builds are remote-first: plain browsers always land in remote
// mode, while the Tauri shell defaults to Desktop. Explicit overrides remain
// for development: ?remote=1 forces remote, ?desktop=1 forces Desktop.
const params = new URLSearchParams(window.location.search);
const isTauriShell =
  typeof (window as unknown as { __TAURI_INTERNALS__?: { invoke?: unknown } }).__TAURI_INTERNALS__?.invoke ===
  "function";
const remoteMode = params.get("remote") === "1" ? true : params.get("desktop") === "1" ? false : !isTauriShell;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <I18nProvider>
      <App remoteMode={remoteMode} />
    </I18nProvider>
  </React.StrictMode>,
);
