import type { AgentSession, InteractionRequestState, LoopInjectionResponse, McpServerSummary, ModelDescriptor, ProviderDescriptor, ProviderPresetDescriptor, SaveSettingsConfigSectionResult, SettingsConfigPayload, SettingsConfigScopeKey, SettingsConfigSectionKey, SidecarEvent, SidecarStatus, TaskGraphItem, TeamLogDetail, TeamMemberActivity, ThinkingLogDetail, ToolLogDetail, ToolLogIndexEntry, TurnStartResponse, WorkspacePathSuggestion } from "../types";
import type { SomniaClient } from "./somnia-client";
import type {
  SessionCreateCommand,
  SessionDeleteCommand,
  SessionListQuery,
  SessionLoadQuery,
  SomniaConnectionListener,
  SomniaConnectionState,
  TurnStartCommand,
} from "./somnia-connection";

interface RemoteSocket {
  onopen: ((event: Event) => unknown) | null;
  onclose: ((event: CloseEvent) => unknown) | null;
  onerror: ((event: Event) => unknown) | null;
  onmessage: ((event: MessageEvent) => unknown) | null;
  send(data: string): void;
  close(): void;
}

interface PendingRequest {
  resolve(value: unknown): void;
  reject(reason: Error): void;
  message: string;
}

interface SequencedEvent {
  kind: "event";
  project_id: string;
  stream_epoch: string;
  sequence: number;
  event: SidecarEvent;
}

const PROTOCOL_VERSION = 1;
// Relay close codes for authentication/authorization failures: retrying these
// can never succeed, so the connection surfaces them instead of looping.
const AUTH_FAILURE_CLOSE_CODES = new Set([4401, 4403]);
const MAX_RECONNECT_DELAY_MS = 30_000;
// A stream_resume (or its reply) can be lost without the socket dropping, e.g.
// when the Relay's bounded send to a busy Connector times out. Without a retry
// the strict-ordering stream would wedge forever, so unanswered resumes are
// re-sent on this interval until a replay/snapshot arrives.
const RESUME_RETRY_DELAY_MS = 2500;

export interface RemoteSomniaConnectionOptions {
  relayUrl: string;
  deviceId: string;
  projectId: string;
  socketFactory?: (url: string) => RemoteSocket;
  reconnectDelayMs?: number;
  /**
   * Called when the Relay closes with an auth failure (4401/4403). Should
   * renew credentials (e.g. rotate the short-lived access cookie via the
   * refresh cookie) and resolve true when a reconnect is worth attempting.
   * Without it, auth failures surface as an unretried error.
   */
  reauthorize?: () => Promise<boolean>;
}

export class RemoteSomniaConnection implements SomniaClient {
  private readonly relayUrl: string;
  private readonly deviceId: string;
  private readonly projectId: string;
  private readonly socketFactory: (url: string) => RemoteSocket;
  private readonly reauthorize: (() => Promise<boolean>) | null;
  private readonly listeners = new Set<SomniaConnectionListener>();
  private readonly pending = new Map<string, PendingRequest>();
  private readonly reconnectDelayMs: number;
  private readonly bufferedEvents = new Map<number, SequencedEvent>();
  private socket: RemoteSocket | null = null;
  private state: SomniaConnectionState = "disconnected";
  private requestSequence = 0;
  private streamEpoch: string | null = null;
  private lastAppliedSequence = 0;
  private lastAcknowledgedSequence = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private resumeRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempt = 0;
  private reauthorizeInFlight = false;
  private explicitlyClosed = false;
  private resumeInFlight = false;

  constructor(options: RemoteSomniaConnectionOptions) {
    this.relayUrl = normalizeRelayUrl(options.relayUrl);
    this.deviceId = required(options.deviceId, "deviceId");
    this.projectId = required(options.projectId, "projectId");
    this.socketFactory = options.socketFactory ?? ((url) => new WebSocket(url));
    this.reconnectDelayMs = Math.max(0, options.reconnectDelayMs ?? 1000);
    this.reauthorize = options.reauthorize ?? null;
  }

  query(query: SessionLoadQuery): Promise<AgentSession>;
  query(query: SessionListQuery): Promise<AgentSession[]>;
  query(query: SessionLoadQuery | SessionListQuery): Promise<AgentSession | AgentSession[]> {
    if (query.type === "session.list") {
      return this.sendRequest<{ sessions: AgentSession[] }>("session.list", {}).then((payload) => payload.sessions);
    }
    return this.sendRequest("session.load", { session_id: query.sessionId });
  }

  execute(command: SessionCreateCommand): Promise<AgentSession>;
  execute(command: SessionDeleteCommand): Promise<{ session_id: string; deleted: boolean }>;
  execute(command: TurnStartCommand): Promise<TurnStartResponse>;
  execute(command: SessionCreateCommand | SessionDeleteCommand | TurnStartCommand): Promise<AgentSession | TurnStartResponse | { session_id: string; deleted: boolean }> {
    if (command.type === "session.create") {
      return this.sendRequest("session.create", {});
    }
    if (command.type === "session.delete") return this.sendRequest("session.delete", { session_id: command.sessionId });
    return this.sendRequest("turn.start", { session_id: command.sessionId, user_input: command.userInput });
  }

  /**
   * Remote devices cannot serve workspace images over plain HTTP, so there is
   * no usable base URL; image rendering resolves through `getWorkspaceImage`.
   */
  get baseUrl(): string {
    return "";
  }

  listSessions(): Promise<AgentSession[]> {
    return this.query({ type: "session.list" });
  }

  createSession(): Promise<AgentSession> {
    return this.execute({ type: "session.create" });
  }

  loadSession(sessionId: string): Promise<AgentSession> {
    return this.query({ type: "session.load", sessionId });
  }

  deleteSession(sessionId: string): Promise<{ session_id: string; deleted: boolean }> {
    return this.execute({ type: "session.delete", sessionId });
  }

  listToolLogs(limit = 24): Promise<ToolLogIndexEntry[]> {
    return this.sendRequest<{ tool_logs: ToolLogIndexEntry[] }>("tool_log.list", { limit }).then((result) => result.tool_logs);
  }

  getToolLog(logId: string): Promise<ToolLogDetail> {
    return this.sendRequest("tool_log.get", { log_id: logId });
  }

  getThinkingLog(path: string): Promise<ThinkingLogDetail> {
    return this.sendRequest("thinking_log.get", { path });
  }

  listActiveTeamMembers(sessionId?: string | null): Promise<TeamMemberActivity[]> {
    return this.sendRequest<{ members: TeamMemberActivity[] }>("team.members", sessionId ? { session_id: sessionId } : {}).then((result) => result.members);
  }

  getTeamLog(name: string, sessionId?: string | null): Promise<TeamLogDetail> {
    return this.sendRequest("team.log", sessionId ? { name, session_id: sessionId } : { name });
  }

  listTasks(sessionId?: string | null): Promise<TaskGraphItem[]> {
    return this.sendRequest<{ tasks: TaskGraphItem[] }>("task.list", sessionId ? { session_id: sessionId } : {}).then((result) => result.tasks);
  }

  getWorkspaceImage(path: string): Promise<string> {
    return this.sendRequest<{ data_url: string }>("workspace.image", { path }).then((result) => result.data_url);
  }

  compactSession(sessionId: string): Promise<{ message: string; session: AgentSession }> {
    return this.sendRequest<{ message: string; session: AgentSession }>("session.compact", { session_id: sessionId });
  }

  janitorSession(sessionId: string): Promise<{ message: string; session: AgentSession }> {
    return this.sendRequest<{ message: string; session: AgentSession }>("session.janitor", { session_id: sessionId });
  }

  listWorkspacePaths(query = "", limit = 30): Promise<WorkspacePathSuggestion[]> {
    return this.sendRequest<{ paths: WorkspacePathSuggestion[] }>("workspace.paths", { query, limit }).then((result) => result.paths);
  }

  stageInlineImage(image: { name: string; mediaType: string; dataUrl: string }): Promise<{ path: string; absolute_path: string; media_type: string }> {
    return this.sendRequest<{ path: string; absolute_path: string; media_type: string }>("workspace.image.stage", {
      name: image.name,
      media_type: image.mediaType,
      data_url: image.dataUrl,
    });
  }

  runtimeStatus(): Promise<SidecarStatus> {
    return this.sendRequest("runtime.status", {});
  }

  listProviders(): Promise<ProviderDescriptor[]> {
    return this.sendRequest<{ providers: ProviderDescriptor[] }>("provider.list", {}).then((result) => result.providers);
  }

  listModels(provider?: string): Promise<ModelDescriptor[]> {
    return this.sendRequest<{ models: ModelDescriptor[] }>("model.list", provider ? { provider } : {}).then((result) => result.models);
  }

  switchProviderModel(providerName: string, model: string): Promise<{ message: string; provider: string; model: string; vision_model?: string | null }> {
    return this.sendRequest("provider.switch", { provider: providerName, model });
  }

  listProviderPresets(): Promise<ProviderPresetDescriptor[]> {
    return this.sendRequest<{ presets: ProviderPresetDescriptor[] }>("provider.presets", {}).then((result) => result.presets);
  }

  debugModelConnection(providerName: string, model: string): Promise<{ provider: string; model: string; ok: boolean; message: string }> {
    return this.sendRequest("provider.debug_model", { provider: providerName, model });
  }

  getSettingsConfig(): Promise<SettingsConfigPayload> {
    return this.sendRequest("settings.config.get", {});
  }

  saveSettingsConfigSection(
    scope: SettingsConfigScopeKey,
    section: SettingsConfigSectionKey,
    content: string,
  ): Promise<SaveSettingsConfigSectionResult> {
    return this.sendRequest("settings.config.save", { scope, section, content });
  }

  listMcpServers(): Promise<McpServerSummary[]> {
    return this.sendRequest<{ servers: McpServerSummary[] }>("mcp.list", {}).then((result) => result.servers);
  }

  debugMcpServer(serverName: string): Promise<{ server: McpServerSummary; tool_count: number }> {
    return this.sendRequest("mcp.debug", { name: serverName });
  }

  setMcpServerEnabled(serverName: string, enabled: boolean): Promise<{ server: McpServerSummary; enabled: boolean; tool_count: number }> {
    return this.sendRequest("mcp.set_enabled", { name: serverName, enabled });
  }

  setVisionModel(visionProvider: string | null, visionModel: string | null, scope: "user" | "project" = "project"): Promise<{ message: string; provider: string; model: string; vision_provider?: string | null; vision_model?: string | null }> {
    return this.sendRequest("vision.set", { provider: visionProvider ?? "", model: visionModel ?? "", scope });
  }

  setReasoningLevel(reasoningLevel: string | null): Promise<{ message: string; provider: string; model: string; vision_model?: string | null; reasoning_level?: string | null }> {
    return this.sendRequest("reasoning.set", { level: reasoningLevel ?? "auto" });
  }

  listInteractions(): Promise<InteractionRequestState[]> {
    return this.sendRequest<{ interactions: InteractionRequestState[] }>("interaction.list", {}).then((result) => result.interactions);
  }

  resolveAuthorization(
    requestId: string,
    options: {
      scope: "once" | "workspace" | "deny";
      approved: boolean;
      reason: string;
    },
  ): Promise<void> {
    const { scope, approved, reason } = options;
    return this.sendRequest("interaction.resolve_authorization", {
      interaction_id: requestId,
      scope,
      approved,
      reason,
    }).then(() => undefined);
  }

  resolveModeSwitch(
    requestId: string,
    options: {
      approved: boolean;
      activeMode?: string;
      reason: string;
    },
  ): Promise<void> {
    const { approved, activeMode, reason } = options;
    return this.sendRequest("interaction.resolve_mode_switch", {
      interaction_id: requestId,
      approved,
      active_mode: activeMode,
      reason,
    }).then(() => undefined);
  }

  setExecutionMode(mode: string): Promise<{ message: string; execution_mode: string; execution_mode_title: string }> {
    return this.sendRequest("execution.mode", { mode });
  }

  interruptTurn(turnId: string): Promise<{ turn_id: string; interrupted: boolean }> {
    return this.sendRequest("turn.interrupt", { turn_id: turnId });
  }

  queueLoopInjection(turnId: string, injectionId: string, userInput: string | Record<string, unknown>): Promise<LoopInjectionResponse> {
    return this.sendRequest("turn.inject", { turn_id: turnId, injection_id: injectionId, user_input: userInput });
  }

  subscribe(listener: SomniaConnectionListener): () => void {
    this.listeners.add(listener);
    if (!this.socket) {
      this.open();
    }
    return () => this.listeners.delete(listener);
  }

  connectionState(): SomniaConnectionState {
    return this.state;
  }

  close(): void {
    this.explicitlyClosed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.clearResumeRetry();
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.setState("disconnected");
    this.rejectPending("Remote connection closed.");
    this.listeners.clear();
  }

  private open(): void {
    if (this.socket || this.explicitlyClosed || this.listeners.size === 0) {
      return;
    }
    this.setState("connecting");
    const url = `${this.relayUrl}/ws/client/${encodeURIComponent(this.deviceId)}`;
    const socket = this.socketFactory(url);
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.reconnectAttempt = 0;
      this.setState("connected");
      this.sendStreamResume();
      this.resendPending();
    };
    socket.onerror = () => this.setState("error", "Relay connection failed.");
    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.clearResumeRetry();
      this.resumeInFlight = false;
      if (AUTH_FAILURE_CLOSE_CODES.has(event.code)) {
        this.handleAuthFailure(event);
        return;
      }
      this.setState("disconnected");
      this.scheduleReconnect();
    };
    socket.onmessage = (messageEvent) => this.handleMessage(String(messageEvent.data));
  }

  private handleMessage(rawMessage: string): void {
    let message: Record<string, unknown>;
    try {
      const decoded = JSON.parse(rawMessage);
      if (!decoded || typeof decoded !== "object") {
        throw new Error("Relay message must be an object.");
      }
      message = decoded as Record<string, unknown>;
    } catch (error) {
      this.notify({ kind: "protocol_error", error: `Ignored malformed Relay message: ${formatError(error)}` });
      return;
    }
    if (message.kind === "response") {
      const requestId = String(message.request_id ?? "");
      const pending = this.pending.get(requestId);
      if (!pending) {
        // Undeliverable control frames (e.g. a stream_resume the Relay could
        // not hand to a busy Connector) come back as failures with an empty
        // request_id. Treat one as a lost resume and retry, otherwise the
        // strict-ordering stream stalls permanently.
        if (!requestId && message.ok === false && this.resumeInFlight) {
          this.sendStreamResume();
        }
        return;
      }
      this.pending.delete(requestId);
      if (message.ok === true) {
        pending.resolve(message.result);
      } else {
        pending.reject(new Error(String(message.error ?? "Remote request failed.")));
      }
      return;
    }
    if (message.kind === "stream_replay") {
      this.handleReplay(message);
      return;
    }
    if (message.kind === "stream_snapshot") {
      this.handleSnapshot(message);
      return;
    }
    if (message.kind === "snapshot_required") {
      this.resumeInFlight = false;
      this.clearResumeRetry();
      this.notify({ kind: "protocol_error", error: String(message.reason ?? "A snapshot resync is required.") });
      return;
    }
    if (message.kind !== "event" || message.project_id !== this.projectId || !isSidecarEvent(message.event)) {
      return;
    }
    const event = message.event;
    if (hasSequence(message)) {
      this.handleSequencedEvent(message as unknown as SequencedEvent);
      return;
    }
    this.publishEvent(event);
  }

  private handleReplay(message: Record<string, unknown>): void {
    if (message.project_id !== this.projectId || typeof message.stream_epoch !== "string" || !Array.isArray(message.events)) {
      this.notify({ kind: "protocol_error", error: "Ignored malformed stream replay." });
      return;
    }
    this.resumeInFlight = false;
    this.clearResumeRetry();
    if (this.streamEpoch !== null && this.streamEpoch !== message.stream_epoch) {
      this.requestSnapshotResume();
      return;
    }
    this.streamEpoch = message.stream_epoch;
    for (const value of message.events) {
      if (isSequencedEvent(value)) this.handleSequencedEvent(value);
    }
  }

  private handleSnapshot(message: Record<string, unknown>): void {
    if (message.project_id !== this.projectId || typeof message.stream_epoch !== "string" || !isRecord(message.snapshot)) {
      this.notify({ kind: "protocol_error", error: "Ignored malformed stream snapshot." });
      return;
    }
    const sequence = integerOrNull(message.sequence);
    if (sequence === null || sequence < 0) {
      this.notify({ kind: "protocol_error", error: "Ignored stream snapshot without a valid sequence." });
      return;
    }
    if (this.streamEpoch === message.stream_epoch && sequence <= this.lastAppliedSequence) {
      this.resumeInFlight = false;
      this.clearResumeRetry();
      return;
    }
    this.resumeInFlight = false;
    this.clearResumeRetry();
    if (this.streamEpoch !== message.stream_epoch) {
      this.lastAcknowledgedSequence = 0;
    }
    this.streamEpoch = message.stream_epoch;
    this.bufferedEvents.clear();
    this.lastAppliedSequence = sequence;
    this.notify({ kind: "snapshot", snapshot: message.snapshot });
    this.sendStreamAck();
  }

  private handleSequencedEvent(message: SequencedEvent): void {
    if (message.project_id !== this.projectId || message.stream_epoch.trim() === "") return;
    if (this.streamEpoch !== null && this.streamEpoch !== message.stream_epoch) {
      this.requestSnapshotResume();
      return;
    }
    this.streamEpoch = message.stream_epoch;
    if (message.sequence <= this.lastAppliedSequence) return;
    if (!this.bufferedEvents.has(message.sequence)) {
      this.bufferedEvents.set(message.sequence, message);
    }
    let applied = false;
    while (this.bufferedEvents.has(this.lastAppliedSequence + 1)) {
      const next = this.bufferedEvents.get(this.lastAppliedSequence + 1)!;
      this.bufferedEvents.delete(next.sequence);
      this.lastAppliedSequence = next.sequence;
      this.publishEvent(next.event);
      applied = true;
    }
    if (applied) this.sendStreamAck();
    if (this.bufferedEvents.size > 0) this.requestResumeIfNeeded();
  }

  private publishEvent(event: SidecarEvent): void {
    if (event.type === "turn_result" && event.session_id) {
      // Deliver the completion immediately. Gating it on an authoritative
      // Session reload meant a hung or lost request swallowed the completion
      // entirely and left the UI stuck on the "answering" indicator.
      this.notify({ kind: "event", event });
      void this.enrichCompletedSession(event);
      return;
    }
    this.notify({ kind: "event", event });
  }

  private async enrichCompletedSession(event: SidecarEvent): Promise<void> {
    try {
      const session = await this.query({ type: "session.load", sessionId: String(event.session_id) });
      this.notify({
        kind: "event",
        event: {
          type: "session_updated",
          session_id: event.session_id ?? null,
          turn_id: event.turn_id ?? null,
          payload: { ...event.payload, session },
        },
      });
    } catch (error) {
      // The completion itself already landed; only the authoritative refresh
      // failed, so surface it as a non-fatal protocol error.
      this.notify({ kind: "protocol_error", error: `Unable to reload completed Session: ${formatError(error)}` });
    }
  }

  private sendRequest<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const socket = this.socket;
    if (!socket || this.state !== "connected") {
      // Transient reconnect windows are normal on mobile networks; hold the
      // request briefly instead of failing the user's click outright.
      return this.waitForConnection().then(() => this.sendRequest<T>(method, params));
    }
    const requestId = `web-${Date.now().toString(36)}-${uniqueRequestSuffix()}-${++this.requestSequence}`;
    const message = JSON.stringify({
      kind: "request",
      protocol_version: PROTOCOL_VERSION,
      device_id: this.deviceId,
      project_id: this.projectId,
      request_id: requestId,
      method,
      params,
    });
    return new Promise<T>((resolve, reject) => {
      this.pending.set(requestId, {
        resolve: (value) => resolve(value as T),
        reject,
        message,
      });
      try {
        socket.send(message);
      } catch (error) {
        this.notify({ kind: "protocol_error", error: `Remote request will retry after reconnect: ${formatError(error)}` });
      }
    });
  }

  private sendStreamResume(): void {
    if (!this.socket || this.state !== "connected") return;
    this.resumeInFlight = true;
    try {
      this.socket.send(JSON.stringify({
        kind: "stream_resume",
        protocol_version: PROTOCOL_VERSION,
        device_id: this.deviceId,
        project_id: this.projectId,
        stream_epoch: this.streamEpoch,
        after_sequence: this.lastAppliedSequence,
      }));
    } catch (error) {
      this.notify({ kind: "protocol_error", error: `Unable to request stream recovery: ${formatError(error)}` });
    }
    this.scheduleResumeRetry();
  }

  private scheduleResumeRetry(): void {
    if (this.resumeRetryTimer !== null) return;
    this.resumeRetryTimer = setTimeout(() => {
      this.resumeRetryTimer = null;
      // The previous resume (or its reply) never landed; ask again instead of
      // letting buffered events pile up forever.
      this.sendStreamResume();
    }, RESUME_RETRY_DELAY_MS);
  }

  private clearResumeRetry(): void {
    if (this.resumeRetryTimer !== null) {
      clearTimeout(this.resumeRetryTimer);
      this.resumeRetryTimer = null;
    }
  }

  private sendStreamAck(): void {
    if (!this.socket || this.streamEpoch === null || this.lastAppliedSequence <= this.lastAcknowledgedSequence) return;
    this.lastAcknowledgedSequence = this.lastAppliedSequence;
    try {
      this.socket.send(JSON.stringify({
        kind: "stream_ack",
        protocol_version: PROTOCOL_VERSION,
        device_id: this.deviceId,
        project_id: this.projectId,
        stream_epoch: this.streamEpoch,
        sequence: this.lastAcknowledgedSequence,
      }));
    } catch (error) {
      this.notify({ kind: "protocol_error", error: `Unable to acknowledge stream progress: ${formatError(error)}` });
    }
  }

  private requestResumeIfNeeded(): void {
    if (!this.resumeInFlight) this.sendStreamResume();
  }

  private requestSnapshotResume(): void {
    this.streamEpoch = null;
    this.lastAppliedSequence = 0;
    this.lastAcknowledgedSequence = 0;
    this.bufferedEvents.clear();
    this.sendStreamResume();
  }

  private resendPending(): void {
    if (!this.socket || this.state !== "connected") return;
    for (const request of this.pending.values()) {
      try {
        this.socket.send(request.message);
      } catch {
        break;
      }
    }
  }

  private waitForConnection(timeoutMs = 10_000): Promise<void> {
    if (this.socket && this.state === "connected") {
      return Promise.resolve();
    }
    if (this.explicitlyClosed) {
      return Promise.reject(new Error("Remote Somnia connection is not connected."));
    }
    return new Promise<void>((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timer);
        unsubscribe();
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error("Remote Somnia connection is not connected."));
      }, timeoutMs);
      const unsubscribe = this.subscribe((notification) => {
        if (notification.kind === "state" && notification.state === "connected") {
          cleanup();
          resolve();
        }
      });
      // subscribe() only opens when the socket is missing; nudge a reconnect
      // in case the drop predated this wait.
      this.open();
    });
  }

  private handleAuthFailure(event: CloseEvent): void {
    const reauthorize = this.reauthorize;
    if (!reauthorize) {
      // No renewal path: retrying can never succeed, so surface instead of looping.
      const detail = event.reason ? ` (${event.reason})` : "";
      this.setState("error", `Relay rejected the connection (${event.code})${detail} Sign in again to reconnect.`);
      return;
    }
    if (this.reauthorizeInFlight) {
      return;
    }
    // The short-lived access cookie expired; renew it via the refresh cookie,
    // then reconnect. Pending requests hold and resend on the new socket.
    this.reauthorizeInFlight = true;
    this.setState("disconnected");
    void reauthorize()
      .then((renewed) => {
        if (renewed) {
          this.reconnectAttempt = 0;
          this.scheduleReconnect();
        } else {
          const detail = event.reason ? ` (${event.reason})` : "";
          this.setState("error", `Relay rejected the connection (${event.code})${detail} Sign in again to reconnect.`);
        }
      })
      .catch(() => {
        // Renewal itself failed transiently; keep retrying with backoff.
        this.scheduleReconnect();
      })
      .finally(() => {
        this.reauthorizeInFlight = false;
      });
  }

  private scheduleReconnect(): void {
    if (this.explicitlyClosed || this.listeners.size === 0 || this.reconnectTimer !== null) return;
    const delay =
      this.reconnectDelayMs === 0 ? 0 : Math.min(this.reconnectDelayMs * 2 ** this.reconnectAttempt, MAX_RECONNECT_DELAY_MS);
    this.reconnectAttempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private setState(state: SomniaConnectionState, error?: string): void {
    if (this.state === state && !error) {
      return;
    }
    this.state = state;
    this.notify({ kind: "state", state, ...(error ? { error } : {}) });
  }

  private notify(notification: Parameters<SomniaConnectionListener>[0]): void {
    for (const listener of this.listeners) {
      listener(notification);
    }
  }

  private rejectPending(message: string): void {
    for (const request of this.pending.values()) {
      request.reject(new Error(message));
    }
    this.pending.clear();
  }
}

function normalizeRelayUrl(rawUrl: string): string {
  const url = new URL(required(rawUrl, "relayUrl"));
  if (url.protocol === "http:") {
    url.protocol = "ws:";
  } else if (url.protocol === "https:") {
    url.protocol = "wss:";
  }
  if (url.protocol !== "ws:" && url.protocol !== "wss:") {
    throw new Error("relayUrl must use http, https, ws, or wss.");
  }
  return url.toString().replace(/\/+$/, "");
}

function isSidecarEvent(value: unknown): value is SidecarEvent {
  if (!value || typeof value !== "object") {
    return false;
  }
  const event = value as Partial<SidecarEvent>;
  return typeof event.type === "string" && Boolean(event.payload) && typeof event.payload === "object";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function integerOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function hasSequence(value: Record<string, unknown>): boolean {
  return typeof value.stream_epoch === "string" && integerOrNull(value.sequence) !== null;
}

function isSequencedEvent(value: unknown): value is SequencedEvent {
  if (!isRecord(value) || value.kind !== "event" || value.project_id === undefined) return false;
  return (
    typeof value.project_id === "string" &&
    typeof value.stream_epoch === "string" &&
    integerOrNull(value.sequence) !== null &&
    isSidecarEvent(value.event)
  );
}

function required(value: string, name: string): string {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    throw new Error(`${name} is required.`);
  }
  return normalized;
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function uniqueRequestSuffix(): string {
  return globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);
}
