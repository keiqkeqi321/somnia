import type {
  AgentSession,
  InteractionRequestState,
  LoopInjectionCancelResponse,
  LoopInjectionResponse,
  McpServerSummary,
  ModelDescriptor,
  ProviderDescriptor,
  ProviderPresetDescriptor,
  SaveSettingsConfigSectionResult,
  SessionModelUpdateResult,
  SettingsConfigPayload,
  SettingsConfigScopeKey,
  SettingsConfigSectionKey,
  SidecarStatus,
  SubagentLogDetail,
  TaskGraphItem,
  TeamLogDetail,
  TeamMemberActivity,
  ThinkingLogDetail,
  ToolLogDetail,
  ToolLogIndexEntry,
  WorkspacePathSuggestion,
} from "../types";
import { SidecarClient } from "./sidecar";
import {
  DirectSomniaConnection,
  type SessionCreateCommand,
  type SessionDeleteCommand,
  type SessionListQuery,
  type SessionLoadQuery,
  type SomniaConnection,
  type SomniaConnectionListener,
  type SomniaConnectionState,
  type TurnStartCommand,
} from "./somnia-connection";

/**
 * Unified client interface consumed by App.tsx. It combines the streaming
 * `SomniaConnection` surface (query/execute/subscribe/connectionState/close)
 * with every REST-style operation the desktop UI needs. Method names and
 * signatures follow `SidecarClient`; both the Direct and Remote adapters
 * implement this interface.
 */
export interface SomniaClient extends SomniaConnection {
  /**
   * HTTP base URL used to resolve workspace image links. The Remote adapter
   * cannot serve workspace images over HTTP and returns an empty string.
   */
  readonly baseUrl: string;

  // Runtime status.
  runtimeStatus(): Promise<SidecarStatus>;

  // Session lifecycle.
  listSessions(): Promise<AgentSession[]>;
  createSession(): Promise<AgentSession>;
  loadSession(sessionId: string): Promise<AgentSession>;
  deleteSession(sessionId: string): Promise<{ session_id: string; deleted: boolean }>;
  compactSession(sessionId: string): Promise<{ message: string; session: AgentSession }>;
  janitorSession(sessionId: string): Promise<{ message: string; session: AgentSession }>;
  /**
   * Pin a session to a provider/model (both set) or clear the pin so the
   * session follows the workspace default (both null). Only this session is
   * affected; the workspace-wide default and other sessions are untouched.
   * `reasoningLevel` is tri-state: `undefined` leaves the model's stored level
   * untouched, `null` clears it to auto, a string sets a concrete level.
   */
  setSessionModel(
    sessionId: string,
    providerName: string | null,
    model: string | null,
    reasoningLevel?: string | null,
  ): Promise<SessionModelUpdateResult>;

  // Turn control.
  interruptTurn(turnId: string): Promise<{ turn_id: string; interrupted: boolean }>;
  queueLoopInjection(
    turnId: string,
    injectionId: string,
    userInput: string | Record<string, unknown>,
  ): Promise<LoopInjectionResponse>;
  cancelLoopInjection(turnId: string, injectionId: string): Promise<LoopInjectionCancelResponse>;
  getSubagentLog(activityId: string): Promise<SubagentLogDetail>;
  // Provider and model controls.
  listProviders(): Promise<ProviderDescriptor[]>;
  listProviderPresets(): Promise<ProviderPresetDescriptor[]>;
  listModels(providerName?: string): Promise<ModelDescriptor[]>;
  switchProviderModel(
    providerName: string,
    model: string,
  ): Promise<{ message: string; provider: string; model: string; vision_model?: string | null }>;
  setVisionModel(
    visionProvider: string | null,
    visionModel: string | null,
    scope?: "user" | "project",
  ): Promise<{ message: string; provider: string; model: string; vision_provider?: string | null; vision_model?: string | null }>;
  setReasoningLevel(
    reasoningLevel: string | null,
  ): Promise<{ message: string; provider: string; model: string; vision_model?: string | null; reasoning_level?: string | null }>;
  debugModelConnection(providerName: string, model: string): Promise<{ provider: string; model: string; ok: boolean; message: string }>;
  setExecutionMode(mode: string): Promise<{ message: string; execution_mode: string; execution_mode_title: string }>;

  // Settings configuration.
  getSettingsConfig(): Promise<SettingsConfigPayload>;
  saveSettingsConfigSection(
    scope: SettingsConfigScopeKey,
    section: SettingsConfigSectionKey,
    content: string,
  ): Promise<SaveSettingsConfigSectionResult>;

  // MCP servers.
  listMcpServers(): Promise<McpServerSummary[]>;
  debugMcpServer(serverName: string): Promise<{ server: McpServerSummary; tool_count: number }>;
  setMcpServerEnabled(
    serverName: string,
    enabled: boolean,
  ): Promise<{ server: McpServerSummary; enabled: boolean; tool_count: number }>;
  setMcpToolEnabled(
    serverName: string,
    toolName: string,
    enabled: boolean,
  ): Promise<{ server: McpServerSummary; tool: string; enabled: boolean; config_path: string }>;

  // Interaction requests.
  listInteractions(): Promise<InteractionRequestState[]>;
  resolveAuthorization(
    requestId: string,
    options: {
      scope: "once" | "workspace" | "deny";
      approved: boolean;
      reason: string;
    },
  ): Promise<void>;
  resolveModeSwitch(
    requestId: string,
    options: {
      approved: boolean;
      activeMode?: string;
      reason: string;
    },
  ): Promise<void>;

  // Tool/thinking/team logs and task graph.
  listToolLogs(limit?: number): Promise<ToolLogIndexEntry[]>;
  getToolLog(logId: string): Promise<ToolLogDetail>;
  getThinkingLog(path: string): Promise<ThinkingLogDetail>;
  getTeamLog(name: string, sessionId?: string | null): Promise<TeamLogDetail>;
  listActiveTeamMembers(sessionId?: string | null): Promise<TeamMemberActivity[]>;
  listTasks(sessionId?: string | null): Promise<TaskGraphItem[]>;

  // Workspace helpers.
  listWorkspacePaths(query?: string, limit?: number): Promise<WorkspacePathSuggestion[]>;
  /**
   * Resolves a workspace image to a renderable `src`. The Direct adapter
   * keeps the plain HTTP URL fast path; the Remote adapter fetches an
   * authenticated data URL over the relay.
   */
  getWorkspaceImage(path: string): Promise<string>;
  stageInlineImage(image: {
    name: string;
    mediaType: string;
    dataUrl: string;
  }): Promise<{ path: string; absolute_path: string; media_type: string }>;
}

/**
 * Direct adapter: a thin wrapper composing the REST `SidecarClient` with a
 * `DirectSomniaConnection` event stream. Every method is a pure delegation.
 */
export class DirectSomniaClient implements SomniaClient {
  private readonly rest: SidecarClient;
  private events: DirectSomniaConnection;

  constructor(baseUrl: string, wsUrl?: string) {
    this.rest = new SidecarClient(baseUrl);
    this.events = new DirectSomniaConnection(this.rest, wsUrl);
  }

  get baseUrl(): string {
    return this.rest.baseUrl;
  }

  /**
   * Points the event stream at the WebSocket URL reported by runtime status.
   * The stream is (re)created lazily on the next `subscribe`.
   */
  setEventStreamUrl(wsUrl: string): void {
    this.events.close();
    this.events = new DirectSomniaConnection(this.rest, wsUrl);
  }

  query(query: SessionLoadQuery): Promise<AgentSession>;
  query(query: SessionListQuery): Promise<AgentSession[]>;
  query(query: SessionLoadQuery | SessionListQuery): Promise<AgentSession | AgentSession[]> {
    return query.type === "session.list" ? this.events.query(query) : this.events.query(query);
  }

  execute(command: SessionCreateCommand): Promise<AgentSession>;
  execute(command: SessionDeleteCommand): Promise<{ session_id: string; deleted: boolean }>;
  execute(command: TurnStartCommand): Promise<{ turn_id: string; session_id: string }>;
  execute(
    command: SessionCreateCommand | SessionDeleteCommand | TurnStartCommand,
  ): Promise<AgentSession | { turn_id: string; session_id: string } | { session_id: string; deleted: boolean }> {
    if (command.type === "session.create") {
      return this.events.execute(command);
    }
    if (command.type === "session.delete") {
      return this.events.execute(command);
    }
    return this.events.execute(command);
  }

  subscribe(listener: SomniaConnectionListener): () => void {
    return this.events.subscribe(listener);
  }

  connectionState(): SomniaConnectionState {
    return this.events.connectionState();
  }

  close(): void {
    this.events.close();
  }

  runtimeStatus(): Promise<SidecarStatus> {
    return this.rest.runtimeStatus();
  }

  listSessions(): Promise<AgentSession[]> {
    return this.rest.listSessions();
  }

  createSession(): Promise<AgentSession> {
    return this.rest.createSession();
  }

  loadSession(sessionId: string): Promise<AgentSession> {
    return this.rest.loadSession(sessionId);
  }

  deleteSession(sessionId: string): Promise<{ session_id: string; deleted: boolean }> {
    return this.rest.deleteSession(sessionId);
  }

  compactSession(sessionId: string): Promise<{ message: string; session: AgentSession }> {
    return this.rest.compactSession(sessionId);
  }

  janitorSession(sessionId: string): Promise<{ message: string; session: AgentSession }> {
    return this.rest.janitorSession(sessionId);
  }

  setSessionModel(
    sessionId: string,
    providerName: string | null,
    model: string | null,
    reasoningLevel?: string | null,
  ): Promise<SessionModelUpdateResult> {
    return this.rest.setSessionModel(sessionId, providerName, model, reasoningLevel);
  }

  interruptTurn(turnId: string): Promise<{ turn_id: string; interrupted: boolean }> {
    return this.rest.interruptTurn(turnId);
  }

  queueLoopInjection(
    turnId: string,
    injectionId: string,
    userInput: string | Record<string, unknown>,
  ): Promise<LoopInjectionResponse> {
    return this.rest.queueLoopInjection(turnId, injectionId, userInput);
  }

  cancelLoopInjection(turnId: string, injectionId: string): Promise<LoopInjectionCancelResponse> {
    return this.rest.cancelLoopInjection(turnId, injectionId);
  }

  getSubagentLog(activityId: string): Promise<SubagentLogDetail> {
    return this.rest.getSubagentLog(activityId);
  }

  listProviders(): Promise<ProviderDescriptor[]> {
    return this.rest.listProviders();
  }

  listProviderPresets(): Promise<ProviderPresetDescriptor[]> {
    return this.rest.listProviderPresets();
  }

  listModels(providerName?: string): Promise<ModelDescriptor[]> {
    return this.rest.listModels(providerName);
  }

  switchProviderModel(
    providerName: string,
    model: string,
  ): Promise<{ message: string; provider: string; model: string; vision_model?: string | null }> {
    return this.rest.switchProviderModel(providerName, model);
  }

  setVisionModel(
    visionProvider: string | null,
    visionModel: string | null,
    scope?: "user" | "project",
  ): Promise<{ message: string; provider: string; model: string; vision_provider?: string | null; vision_model?: string | null }> {
    return this.rest.setVisionModel(visionProvider, visionModel, scope);
  }

  setReasoningLevel(
    reasoningLevel: string | null,
  ): Promise<{ message: string; provider: string; model: string; vision_model?: string | null; reasoning_level?: string | null }> {
    return this.rest.setReasoningLevel(reasoningLevel);
  }

  debugModelConnection(providerName: string, model: string): Promise<{ provider: string; model: string; ok: boolean; message: string }> {
    return this.rest.debugModelConnection(providerName, model);
  }

  setExecutionMode(mode: string): Promise<{ message: string; execution_mode: string; execution_mode_title: string }> {
    return this.rest.setExecutionMode(mode);
  }

  getSettingsConfig(): Promise<SettingsConfigPayload> {
    return this.rest.getSettingsConfig();
  }

  saveSettingsConfigSection(
    scope: SettingsConfigScopeKey,
    section: SettingsConfigSectionKey,
    content: string,
  ): Promise<SaveSettingsConfigSectionResult> {
    return this.rest.saveSettingsConfigSection(scope, section, content);
  }

  listMcpServers(): Promise<McpServerSummary[]> {
    return this.rest.listMcpServers();
  }

  debugMcpServer(serverName: string): Promise<{ server: McpServerSummary; tool_count: number }> {
    return this.rest.debugMcpServer(serverName);
  }

  setMcpServerEnabled(
    serverName: string,
    enabled: boolean,
  ): Promise<{ server: McpServerSummary; enabled: boolean; tool_count: number }> {
    return this.rest.setMcpServerEnabled(serverName, enabled);
  }

  setMcpToolEnabled(
    serverName: string,
    toolName: string,
    enabled: boolean,
  ): Promise<{ server: McpServerSummary; tool: string; enabled: boolean; config_path: string }> {
    return this.rest.setMcpToolEnabled(serverName, toolName, enabled);
  }

  listInteractions(): Promise<InteractionRequestState[]> {
    return this.rest.listInteractions();
  }

  resolveAuthorization(
    requestId: string,
    options: {
      scope: "once" | "workspace" | "deny";
      approved: boolean;
      reason: string;
    },
  ): Promise<void> {
    return this.rest.resolveAuthorization(requestId, options);
  }

  resolveModeSwitch(
    requestId: string,
    options: {
      approved: boolean;
      activeMode?: string;
      reason: string;
    },
  ): Promise<void> {
    return this.rest.resolveModeSwitch(requestId, options);
  }

  listToolLogs(limit?: number): Promise<ToolLogIndexEntry[]> {
    return this.rest.listToolLogs(limit);
  }

  getToolLog(logId: string): Promise<ToolLogDetail> {
    return this.rest.getToolLog(logId);
  }

  getThinkingLog(path: string): Promise<ThinkingLogDetail> {
    return this.rest.getThinkingLog(path);
  }

  getTeamLog(name: string, sessionId?: string | null): Promise<TeamLogDetail> {
    return this.rest.getTeamLog(name, sessionId);
  }

  listActiveTeamMembers(sessionId?: string | null): Promise<TeamMemberActivity[]> {
    return this.rest.listActiveTeamMembers(sessionId);
  }

  listTasks(sessionId?: string | null): Promise<TaskGraphItem[]> {
    return this.rest.listTasks(sessionId);
  }

  listWorkspacePaths(query?: string, limit?: number): Promise<WorkspacePathSuggestion[]> {
    return this.rest.listWorkspacePaths(query, limit);
  }

  getWorkspaceImage(path: string): Promise<string> {
    return Promise.resolve(this.rest.workspaceImageUrl(path));
  }

  stageInlineImage(image: {
    name: string;
    mediaType: string;
    dataUrl: string;
  }): Promise<{ path: string; absolute_path: string; media_type: string }> {
    return this.rest.stageInlineImage(image);
  }
}
