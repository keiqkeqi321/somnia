import type {
  AgentSession,
  InteractionRequestState,
  LoopInjectionCancelResponse,
  LoopInjectionResponse,
  ModelDescriptor,
  McpServerSummary,
  ProviderDescriptor,
  ProviderPresetDescriptor,
  RemoteDeviceStatus,
  RemoteProjectTarget,
  SaveSettingsConfigSectionResult,
  SessionModelUpdateResult,
  SettingsConfigPayload,
  SettingsConfigScopeKey,
  SettingsConfigSectionKey,
  SidecarStatus,
  SubagentLogDetail,
  TaskGraphItem,
  TeamMemberActivity,
  TeamLogDetail,
  ThinkingLogDetail,
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

  async compactSession(sessionId: string): Promise<{ message: string; session: AgentSession }> {
    return parseResponse<{ message: string; session: AgentSession }>(
      await fetch(`${this.baseUrl}/sessions/${sessionId}/compact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  }

  async janitorSession(sessionId: string): Promise<{ message: string; session: AgentSession }> {
    return parseResponse<{ message: string; session: AgentSession }>(
      await fetch(`${this.baseUrl}/sessions/${sessionId}/janitor`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  }

  /**
   * Pin a session to a specific provider/model, or clear the pin (pass null for
   * both) so it follows the workspace default. Only this session is affected.
   * `reasoningLevel` is tri-state: `undefined` leaves the model's stored level
   * untouched, `null` clears it to auto, a string sets a concrete level.
   */
  async setSessionModel(
    sessionId: string,
    providerName: string | null,
    model: string | null,
    reasoningLevel?: string | null,
  ): Promise<SessionModelUpdateResult> {
    return parseResponse<SessionModelUpdateResult>(
      await fetch(`${this.baseUrl}/sessions/${sessionId}/model`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider_name: providerName ?? "",
          model: model ?? "",
          ...(reasoningLevel !== undefined ? { reasoning_level: reasoningLevel } : {}),
        }),
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

  async cancelLoopInjection(turnId: string, injectionId: string): Promise<LoopInjectionCancelResponse> {
    return parseResponse<LoopInjectionCancelResponse>(
      await fetch(`${this.baseUrl}/turns/${turnId}/loop-injections/${encodeURIComponent(injectionId)}`, {
        method: "DELETE",
      }),
    );
  }

  async getSubagentLog(activityId: string): Promise<SubagentLogDetail> {
    const payload = await parseResponse<{ subagent_log: SubagentLogDetail }>(
      await fetch(`${this.baseUrl}/subagent-logs/${encodeURIComponent(activityId)}`),
    );
    return payload.subagent_log;
  }

  async listProviders(): Promise<ProviderDescriptor[]> {
    const payload = await parseResponse<{ providers: ProviderDescriptor[] }>(await fetch(`${this.baseUrl}/providers`));
    return payload.providers;
  }

  async listProviderPresets(): Promise<ProviderPresetDescriptor[]> {
    const payload = await parseResponse<{ presets: ProviderPresetDescriptor[] }>(await fetch(`${this.baseUrl}/provider-presets`));
    return payload.presets;
  }

  async listModels(providerName?: string): Promise<ModelDescriptor[]> {
    const query = providerName ? `?provider=${encodeURIComponent(providerName)}` : "";
    const payload = await parseResponse<{ models: ModelDescriptor[] }>(await fetch(`${this.baseUrl}/models${query}`));
    return payload.models;
  }

  async getSettingsConfig(): Promise<SettingsConfigPayload> {
    return parseResponse<SettingsConfigPayload>(await fetch(`${this.baseUrl}/settings/config`));
  }

  async listMcpServers(): Promise<McpServerSummary[]> {
    const payload = await parseResponse<{ servers: McpServerSummary[] }>(await fetch(`${this.baseUrl}/mcp/servers`));
    return payload.servers;
  }

  async debugMcpServer(serverName: string): Promise<{ server: McpServerSummary; tool_count: number }> {
    return parseResponse<{ server: McpServerSummary; tool_count: number }>(
      await fetch(`${this.baseUrl}/mcp/servers/${encodeURIComponent(serverName)}/debug`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  }

  async setMcpServerEnabled(serverName: string, enabled: boolean): Promise<{ server: McpServerSummary; enabled: boolean; tool_count: number }> {
    return parseResponse<{ server: McpServerSummary; enabled: boolean; tool_count: number }>(
      await fetch(`${this.baseUrl}/mcp/servers/${encodeURIComponent(serverName)}/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      }),
    );
  }

  async setMcpToolEnabled(serverName: string, toolName: string, enabled: boolean): Promise<{ server: McpServerSummary; tool: string; enabled: boolean; config_path: string }> {
    return parseResponse<{ server: McpServerSummary; tool: string; enabled: boolean; config_path: string }>(
      await fetch(`${this.baseUrl}/mcp/servers/${encodeURIComponent(serverName)}/tools/${encodeURIComponent(toolName)}/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      }),
    );
  }

  async saveSettingsConfigSection(
    scope: SettingsConfigScopeKey,
    section: SettingsConfigSectionKey,
    content: string,
  ): Promise<SaveSettingsConfigSectionResult> {
    return parseResponse<SaveSettingsConfigSectionResult>(
      await fetch(`${this.baseUrl}/settings/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, section, content }),
      }),
    );
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

  workspaceImageUrl(path: string): string {
    return `${this.baseUrl}/workspace/images?path=${encodeURIComponent(path)}`;
  }

  async switchProviderModel(providerName: string, model: string): Promise<{ message: string; provider: string; model: string; vision_model?: string | null }> {
    return parseResponse<{ message: string; provider: string; model: string; vision_model?: string | null }>(
      await fetch(`${this.baseUrl}/providers/switch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_name: providerName, model }),
      }),
    );
  }

  async setVisionModel(
    visionProvider: string | null,
    visionModel: string | null,
    scope: "user" | "project" = "project",
  ): Promise<{ message: string; provider: string; model: string; vision_provider?: string | null; vision_model?: string | null }> {
    return parseResponse<{ message: string; provider: string; model: string; vision_provider?: string | null; vision_model?: string | null }>(
      await fetch(`${this.baseUrl}/vision-model`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, vision_provider: visionProvider ?? "", vision_model: visionModel ?? "" }),
      }),
    );
  }

  async setReasoningLevel(reasoningLevel: string | null): Promise<{ message: string; provider: string; model: string; vision_model?: string | null; reasoning_level?: string | null }> {
    return parseResponse<{ message: string; provider: string; model: string; vision_model?: string | null; reasoning_level?: string | null }>(
      await fetch(`${this.baseUrl}/reasoning`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reasoning_level: reasoningLevel ?? "auto" }),
      }),
    );
  }

  async debugModelConnection(providerName: string, model: string): Promise<{ provider: string; model: string; ok: boolean; message: string }> {
    return parseResponse<{ provider: string; model: string; ok: boolean; message: string }>(
      await fetch(`${this.baseUrl}/providers/debug-model`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_name: providerName, model }),
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

  async getThinkingLog(path: string): Promise<ThinkingLogDetail> {
    const payload = await parseResponse<{ thinking_log: ThinkingLogDetail }>(
      await fetch(`${this.baseUrl}/thinking-log?path=${encodeURIComponent(path)}`),
    );
    return payload.thinking_log;
  }

  async getTeamLog(name: string, sessionId?: string | null): Promise<TeamLogDetail> {
    const params = new URLSearchParams({ name });
    if (sessionId) {
      params.set("session_id", sessionId);
    }
    const payload = await parseResponse<{ team_log: TeamLogDetail }>(await fetch(`${this.baseUrl}/team/log?${params}`));
    return payload.team_log;
  }

  async listActiveTeamMembers(sessionId?: string | null): Promise<TeamMemberActivity[]> {
    const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
    const payload = await parseResponse<{ members: TeamMemberActivity[] }>(await fetch(`${this.baseUrl}/team/active${query}`));
    return payload.members;
  }

  async listTasks(sessionId?: string | null): Promise<TaskGraphItem[]> {
    const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
    const payload = await parseResponse<{ tasks: TaskGraphItem[] }>(await fetch(`${this.baseUrl}/tasks${query}`));
    return payload.tasks;
  }

  async getRemoteStatus(): Promise<RemoteDeviceStatus> {
    return parseResponse<RemoteDeviceStatus>(await fetch(`${this.baseUrl}/remote/status`));
  }

  async pairBeginRemoteDevice(relayUrl: string): Promise<RemoteDeviceStatus> {
    return parseResponse<RemoteDeviceStatus>(
      await fetch(`${this.baseUrl}/remote/pair-begin`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ relay_url: relayUrl }),
      }),
    );
  }

  async pairCancelRemoteDevice(): Promise<RemoteDeviceStatus> {
    return parseResponse<RemoteDeviceStatus>(
      await fetch(`${this.baseUrl}/remote/pair-cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  }

  async enableRemoteDevice(projects?: RemoteProjectTarget[]): Promise<RemoteDeviceStatus> {
    return parseResponse<RemoteDeviceStatus>(
      await fetch(`${this.baseUrl}/remote/enable`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: projects && projects.length > 0 ? JSON.stringify({ projects }) : "{}",
      }),
    );
  }

  async getRemoteProjectId(): Promise<string> {
    const payload = await parseResponse<{ project_id: string }>(await fetch(`${this.baseUrl}/remote/project-id`));
    return payload.project_id;
  }

  async disableRemoteDevice(): Promise<RemoteDeviceStatus> {
    return parseResponse<RemoteDeviceStatus>(
      await fetch(`${this.baseUrl}/remote/disable`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  }

  async unpairRemoteDevice(): Promise<RemoteDeviceStatus> {
    return parseResponse<RemoteDeviceStatus>(
      await fetch(`${this.baseUrl}/remote/unpair`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    );
  }
}

export { buildWebSocketUrl, normalizeBaseUrl };
