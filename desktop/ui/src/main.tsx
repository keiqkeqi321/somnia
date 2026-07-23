import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import RemoteTracerApp from "./RemoteTracerApp";
import { I18nProvider } from "./lib/i18n";
import "./styles.css";

const remoteMode = new URLSearchParams(window.location.search).get("remote") === "1";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <I18nProvider>
      {remoteMode ? <RemoteTracerApp /> : <App />}
    </I18nProvider>
  </React.StrictMode>,
);
