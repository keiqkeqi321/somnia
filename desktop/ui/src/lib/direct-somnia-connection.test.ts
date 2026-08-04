import { vi } from "vitest";

import type { AgentSession, SidecarEvent } from "../types";
import { DirectSomniaClient } from "./somnia-client";
import {
  contractMcpServers,
  contractProviderPresets,
  contractRuntimeStatus,
  contractSetMcpToolEnabledResult,
  contractSettingsConfig,
  contractSettingsSaveResult,
  contractTasks,
  contractToolLogDetail,
  contractToolLogs,
  contractWorkspacePaths,
  describeSomniaConnectionContract,
} from "./somnia-connection.contract";

const loadedSession: AgentSession = {
  id: "session-1",
  messages: [{ role: "user", content: "Initial question" }],
  token_usage: {},
  todo_items: [],
  rounds_without_todo: 0,
};

class FakeEventSocket {
  onopen: ((event: Event) => unknown) | null = null;
  onclose: ((event: CloseEvent) => unknown) | null = null;
  onerror: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent) => unknown) | null = null;

  open() {
    this.onopen?.(new Event("open"));
  }

  emit(event: SidecarEvent) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
  }

  close() {
    this.onclose?.(new Event("close") as CloseEvent);
  }
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function stubSidecarFetch(): void {
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.endsWith("/sessions") && method === "POST") return jsonResponse({ session: loadedSession });
    if (url.endsWith("/sessions") && method === "GET") return jsonResponse({ sessions: [loadedSession] });
    if (url.includes("/sessions/") && method === "DELETE") return jsonResponse({ session_id: loadedSession.id, deleted: true });
    if (url.includes("/sessions/") && method === "GET") return jsonResponse({ session: loadedSession });
    if (url.endsWith("/turns") && method === "POST") return jsonResponse({ turn_id: "turn-1", session_id: loadedSession.id });
    if (url.endsWith("/runtime/status")) return jsonResponse(contractRuntimeStatus);
    if (url.includes("/workspace/paths")) return jsonResponse({ paths: contractWorkspacePaths });
    if (url.includes("/tasks")) return jsonResponse({ tasks: contractTasks });
    if (url.endsWith("/settings/config") && method === "GET") return jsonResponse(contractSettingsConfig);
    if (url.endsWith("/settings/config") && method === "POST") return jsonResponse(contractSettingsSaveResult);
    if (url.endsWith("/provider-presets")) return jsonResponse({ presets: contractProviderPresets });
    if (url.endsWith("/mcp/servers")) return jsonResponse({ servers: contractMcpServers });
    if (url.includes("/mcp/servers/") && url.includes("/tools/") && method === "POST") return jsonResponse(contractSetMcpToolEnabledResult);
    if (url.includes("/authorization")) return jsonResponse({ resolved: true });
    if (url.includes("/tool-logs/log-1")) return jsonResponse({ tool_log: contractToolLogDetail });
    if (url.includes("/tool-logs")) return jsonResponse({ tool_logs: contractToolLogs });
    throw new Error(`Unexpected fetch: ${method} ${url}`);
  });
}

describeSomniaConnectionContract(
  "Direct",
  () => {
    const socket = new FakeEventSocket();
    vi.stubGlobal("WebSocket", function () {
      return socket;
    });
    stubSidecarFetch();
    return {
      connection: new DirectSomniaClient("http://sidecar.test", "ws://sidecar.test/ws"),
      openStream: () => socket.open(),
      emitEvent: (event) => socket.emit(event),
      closeStream: () => socket.close(),
    };
  },
  loadedSession,
);
