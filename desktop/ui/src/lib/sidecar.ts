import type {
  AgentSession,
  InteractionRequestState,
  LoopInjectionResponse,
  ModelDescriptor,
  ProviderDescriptor,
  SidecarStatus,
  ToolLogDetail,
  ToolLogIndexEntry,
  TurnStartResponse,
  WorkspacePathSuggestion,
} from "../types";

function normalizeBaseUrl(rawBaseUrl: string): string {
  const trimmed = rawBaseUrl.trim();
  if (!trimmed) {
    return "http://127.0.0.1:8765";
  }
  if (/^https?:\/\//i.test(trimmed)) {
    return trimmed.replace(/\/+$/, "");
  }
  return `http://${trimmed.replace(/\/+$/, "")}`;
}

function buildWebSocketUrl(baseUrl: string): string {
  const url = new URL(normalizeBaseUrl(baseUrl));
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws";
  url.search = "";
  url.hash = "";
  return url.toString();
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { error?: string };
      if (payload.error) {
        message = payload.error;
      }
    } catch {
      // Ignore body parse failures for non-JSON responses.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export class SidecarClient {
  readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
  }

  createEventSocket(wsUrl?: string): WebSocket {
    return new WebSocket(wsUrl?.trim() ? wsUrl : buildWebSocketUrl(this.baseUrl));
  }

  async health(): Promise<SidecarStatus> {
    return parseResponse<SidecarStatus>(await fetch(`${this.baseUrl}/health`));
  }

  async runtimeStatus(): Promise<SidecarStatus> {
    return parseResponse<SidecarStatus>(await fetch(`${this.baseUrl}/runtime/status`));
  }

  async listSessions(): Promise<AgentSession[]> {
    const payload = await parseResponse<{ sessions: AgentSession[] }>(await fetch(`${this.baseUrl}/sessions`));
    return payload.sessions;
  }

  async createSession(): Promise<AgentSession> {
    const payload = await parseResponse<{ session: AgentSession }>(
      await fetch(`${this.baseUrl}/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
    return payload.session;
  }

  async loadSession(sessionId: string): Promise<AgentSession> {
    const payload = await parseResponse<{ session: AgentSession }>(await fetch(`${this.baseUrl}/sessions/${sessionId}`));
    return payload.session;
  }

  async deleteSession(sessionId: string): Promise<{ session_id: string; deleted: boolean }> {
    return parseResponse<{ session_id: string; deleted: boolean }>(
      await fetch(`${this.baseUrl}/sessions/${sessionId}`, {
        method: "DELETE",
      }),
    );
  }

  async startTurn(sessionId: string, userInput: string | Record<string, unknown>): Promise<TurnStartResponse> {
    return parseResponse<TurnStartResponse>(
      await fetch(`${this.baseUrl}/turns`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, user_input: userInput }),
      }),
    );
  }

  async interruptTurn(turnId: string): Promise<{ turn_id: string; interrupted: boolean }> {
    return parseResponse<{ turn_id: string; interrupted: boolean }>(
      await fetch(`${this.baseUrl}/turns/${turnId}/interrupt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  }

  async queueLoopInjection(
    turnId: string,
    injectionId: string,
    userInput: string | Record<string, unknown>,
  ): Promise<LoopInjectionResponse> {
    return parseResponse<LoopInjectionResponse>(
      await fetch(`${this.baseUrl}/turns/${turnId}/loop-injections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ injection_id: injectionId, user_input: userInput }),
      }),
    );
  }

  async listProviders(): Promise<ProviderDescriptor[]> {
    const payload = await parseResponse<{ providers: ProviderDescriptor[] }>(await fetch(`${this.baseUrl}/providers`));
    return payload.providers;
  }

  async listModels(providerName?: string): Promise<ModelDescriptor[]> {
    const query = providerName ? `?provider=${encodeURIComponent(providerName)}` : "";
    const payload = await parseResponse<{ models: ModelDescriptor[] }>(await fetch(`${this.baseUrl}/models${query}`));
    return payload.models;
  }

  async listWorkspacePaths(query = "", limit = 30): Promise<WorkspacePathSuggestion[]> {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    const payload = await parseResponse<{ paths: WorkspacePathSuggestion[] }>(await fetch(`${this.baseUrl}/workspace/paths?${params}`));
    return payload.paths;
  }

  async stageInlineImage(
    image: {
      name: string;
      mediaType: string;
      dataUrl: string;
    },
  ): Promise<{ path: string; absolute_path: string; media_type: string }> {
    return parseResponse<{ path: string; absolute_path: string; media_type: string }>(
      await fetch(`${this.baseUrl}/workspace/images`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: image.name,
          media_type: image.mediaType,
          data_url: image.dataUrl,
        }),
      }),
    );
  }

  async switchProviderModel(providerName: string, model: string): Promise<{ message: string; provider: string; model: string }> {
    return parseResponse<{ message: string; provider: string; model: string }>(
      await fetch(`${this.baseUrl}/providers/switch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_name: providerName, model }),
      }),
    );
  }

  async setReasoningLevel(reasoningLevel: string | null): Promise<{ message: string; provider: string; model: string; reasoning_level?: string | null }> {
    return parseResponse<{ message: string; provider: string; model: string; reasoning_level?: string | null }>(
      await fetch(`${this.baseUrl}/reasoning`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reasoning_level: reasoningLevel ?? "auto" }),
      }),
    );
  }

  async setExecutionMode(mode: string): Promise<{ message: string; execution_mode: string; execution_mode_title: string }> {
    return parseResponse<{ message: string; execution_mode: string; execution_mode_title: string }>(
      await fetch(`${this.baseUrl}/execution-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      }),
    );
  }

  async listInteractions(): Promise<InteractionRequestState[]> {
    const payload = await parseResponse<{ interactions: InteractionRequestState[] }>(await fetch(`${this.baseUrl}/interactions`));
    return payload.interactions;
  }

  async resolveAuthorization(
    requestId: string,
    options: {
      scope: "once" | "workspace" | "deny";
      approved: boolean;
      reason: string;
    },
  ): Promise<void> {
    const { scope, approved, reason } = options;
    await parseResponse<{ resolved: boolean }>(
      await fetch(`${this.baseUrl}/interactions/${requestId}/authorization`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, approved, reason }),
      }),
    );
  }

  async resolveModeSwitch(
    requestId: string,
    options: {
      approved: boolean;
      activeMode?: string;
      reason: string;
    },
  ): Promise<void> {
    const { approved, activeMode, reason } = options;
    await parseResponse<{ resolved: boolean }>(
      await fetch(`${this.baseUrl}/interactions/${requestId}/mode-switch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved, active_mode: activeMode, reason }),
      }),
    );
  }

  async listToolLogs(limit = 24): Promise<ToolLogIndexEntry[]> {
    const payload = await parseResponse<{ tool_logs: ToolLogIndexEntry[] }>(
      await fetch(`${this.baseUrl}/tool-logs?limit=${encodeURIComponent(String(limit))}`),
    );
    return payload.tool_logs;
  }

  async getToolLog(logId: string): Promise<ToolLogDetail> {
    const payload = await parseResponse<{ tool_log: ToolLogDetail }>(await fetch(`${this.baseUrl}/tool-logs/${logId}`));
    return payload.tool_log;
  }
}

export { buildWebSocketUrl, normalizeBaseUrl };
