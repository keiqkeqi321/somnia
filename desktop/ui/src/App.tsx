import { useEffect, useRef, useState } from "react";

import { buildConversationRows, buildSessionPreview, formatRelativeTime, formatTodoLabel, sortSessions } from "./lib/messages";
import { SidecarClient, normalizeBaseUrl } from "./lib/sidecar";
import type {
  AgentSession,
  ConversationRow,
  InteractionRequestState,
  ModelDescriptor,
  ProviderDescriptor,
  SidecarEvent,
  SidecarStatus,
  ToolLogDetail,
  ToolLogIndexEntry,
} from "./types";

const STORAGE_KEY = "somnia.desktop.sidecar-url";
const DEFAULT_SIDECAR_URL = "http://127.0.0.1:8765";
const TOOL_LIMIT = 24;

function App() {
  const initialBaseUrl =
    (typeof window !== "undefined" && window.localStorage.getItem(STORAGE_KEY)) || DEFAULT_SIDECAR_URL;
  const [baseUrlInput, setBaseUrlInput] = useState(initialBaseUrl);
  const [connectionState, setConnectionState] = useState<"connecting" | "connected" | "disconnected" | "error">(
    "disconnected",
  );
  const [status, setStatus] = useState<SidecarStatus | null>(null);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [currentSession, setCurrentSession] = useState<AgentSession | null>(null);
  const [draft, setDraft] = useState("");
  const [streamingText, setStreamingText] = useState("");
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [pendingInteractions, setPendingInteractions] = useState<InteractionRequestState[]>([]);
  const [providers, setProviders] = useState<ProviderDescriptor[]>([]);
  const [models, setModels] = useState<ModelDescriptor[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [toolLogs, setToolLogs] = useState<ToolLogIndexEntry[]>([]);
  const [activeToolLog, setActiveToolLog] = useState<ToolLogDetail | null>(null);
  const [inspectorTab, setInspectorTab] = useState<"todos" | "logs" | "approvals">("todos");
  const [bannerMessage, setBannerMessage] = useState("Point the UI at a running sidecar and start a session.");
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const clientRef = useRef<SidecarClient | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const currentSessionRef = useRef<AgentSession | null>(null);

  selectedSessionIdRef.current = selectedSessionId;
  currentSessionRef.current = currentSession;

  useEffect(() => {
    void connectToSidecar(initialBaseUrl);
    return () => {
      socketRef.current?.close();
    };
    // Intentionally run only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connectToSidecar(nextBaseUrl = baseUrlInput) {
    const normalizedBaseUrl = normalizeBaseUrl(nextBaseUrl);
    setConnectionState("connecting");
    setBannerMessage("Connecting to sidecar...");
    socketRef.current?.close();

    try {
      const nextClient = new SidecarClient(normalizedBaseUrl);
      const runtimeStatus = await nextClient.runtimeStatus();
      const [sessionList, providerList, interactionList, logList] = await Promise.all([
        nextClient.listSessions(),
        nextClient.listProviders(),
        nextClient.listInteractions(),
        nextClient.listToolLogs(TOOL_LIMIT),
      ]);

      clientRef.current = nextClient;
      setStatus(runtimeStatus);
      setConnectionState("connected");
      setPendingInteractions(interactionList);
      setToolLogs(logList);
      setProviders(providerList);
      setSelectedProvider(runtimeStatus.provider);
      setBannerMessage(`Connected to ${runtimeStatus.base_url}`);
      if (typeof window !== "undefined") {
        window.localStorage.setItem(STORAGE_KEY, normalizedBaseUrl);
      }

      const sortedSessions = sortSessions(sessionList);
      setSessions(sortedSessions);
      const nextSessionId =
        sortedSessions.find((session) => session.id === selectedSessionIdRef.current)?.id ?? sortedSessions[0]?.id ?? null;
      if (nextSessionId) {
        await selectSession(nextSessionId, nextClient, sortedSessions);
      } else {
        setSelectedSessionId(null);
        setCurrentSession(null);
        setStreamingText("");
      }
      await refreshModels(runtimeStatus.provider, nextClient, runtimeStatus.model);
      openEventSocket(nextClient, runtimeStatus.ws_url);
    } catch (error) {
      clientRef.current = null;
      setConnectionState("error");
      setBannerMessage(formatErrorMessage(error));
      setStatus(null);
    }
  }

  function openEventSocket(client: SidecarClient, wsUrl: string) {
    const socket = client.createEventSocket(wsUrl);
    socketRef.current = socket;

    socket.onopen = () => {
      setConnectionState("connected");
    };

    socket.onclose = () => {
      if (clientRef.current === client) {
        setConnectionState("disconnected");
        setBannerMessage("Sidecar event stream disconnected.");
      }
    };

    socket.onerror = () => {
      setConnectionState("error");
      setBannerMessage("Sidecar event stream failed.");
    };

    socket.onmessage = (messageEvent) => {
      try {
        const event = JSON.parse(String(messageEvent.data)) as SidecarEvent;
        void handleSidecarEvent(event);
      } catch (error) {
        setBannerMessage(`Ignored malformed sidecar event: ${formatErrorMessage(error)}`);
      }
    };
  }

  async function handleSidecarEvent(event: SidecarEvent) {
    if (event.type === "sidecar_ready") {
      return;
    }
    if (event.type === "session_created") {
      const payloadSession = readSessionFromPayload(event.payload.session);
      if (payloadSession) {
        upsertSession(payloadSession);
        setSelectedSessionId(payloadSession.id);
        setCurrentSession(payloadSession);
      }
      return;
    }
    if (event.type === "turn_started") {
      if (event.session_id && event.session_id === selectedSessionIdRef.current) {
        setStreamingText("");
      }
      setActiveTurnId(event.turn_id ?? null);
      return;
    }
    if (event.type === "assistant_delta") {
      if (!selectedSessionIdRef.current || event.session_id !== selectedSessionIdRef.current) {
        return;
      }
      const delta = typeof event.payload.delta === "string" ? event.payload.delta : "";
      if (delta) {
        setStreamingText((previous) => previous + delta);
      }
      return;
    }
    if (event.type === "session_updated") {
      const payloadSession = readSessionFromPayload(event.payload.session);
      if (payloadSession) {
        upsertSession(payloadSession);
        if (payloadSession.id === selectedSessionIdRef.current) {
          setCurrentSession(payloadSession);
        }
      }
      return;
    }
    if (event.type === "todo_updated") {
      const items = Array.isArray(event.payload.items) ? event.payload.items : null;
      const session = currentSessionRef.current;
      if (!items || !session || event.session_id !== session.id) {
        return;
      }
      const nextSession = { ...session, todo_items: items } as AgentSession;
      setCurrentSession(nextSession);
      upsertSession(nextSession);
      return;
    }
    if (event.type === "tool_finished") {
      void refreshToolLogs();
      return;
    }
    if (event.type === "provider_switched" || event.type === "reasoning_level_updated") {
      void refreshStatusAndProviders();
      return;
    }
    if (event.type === "authorization_requested" || event.type === "mode_switch_requested") {
      setInspectorTab("approvals");
      void refreshInteractions();
      return;
    }
    if (event.type === "interrupt_completed" || event.type === "error") {
      setActiveTurnId((current) => (current === event.turn_id ? null : current));
      setStreamingText("");
      void refreshInteractions();
      void refreshStatusAndProviders();
      return;
    }
    if (event.type === "turn_result") {
      setActiveTurnId((current) => (current === event.turn_id ? null : current));
      setStreamingText("");
      const payloadSession = readSessionFromPayload(event.payload.session);
      if (payloadSession) {
        upsertSession(payloadSession);
        if (payloadSession.id === selectedSessionIdRef.current) {
          setCurrentSession(payloadSession);
        }
      }
      void refreshInteractions();
      void refreshToolLogs();
      void refreshStatusAndProviders();
    }
  }

  async function refreshStatusAndProviders() {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    const [runtimeStatus, providerList, interactionList] = await Promise.all([
      client.runtimeStatus(),
      client.listProviders(),
      client.listInteractions(),
    ]);
    setStatus(runtimeStatus);
    setProviders(providerList);
    setPendingInteractions(interactionList);
    setSelectedProvider(runtimeStatus.provider);
    await refreshModels(runtimeStatus.provider, client, runtimeStatus.model);
  }

  async function refreshModels(providerName: string, client = clientRef.current, preferredModel?: string) {
    if (!client) {
      return;
    }
    const nextModels = await client.listModels(providerName);
    setModels(nextModels);
    const activeModel = preferredModel ?? nextModels.find((model) => model.is_active)?.name ?? nextModels[0]?.name ?? "";
    setSelectedModel(activeModel);
  }

  async function refreshInteractions() {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    setPendingInteractions(await client.listInteractions());
  }

  async function refreshToolLogs() {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    const nextLogs = await client.listToolLogs(TOOL_LIMIT);
    setToolLogs(nextLogs);
    if (activeToolLog) {
      try {
        setActiveToolLog(await client.getToolLog(activeToolLog.id));
      } catch {
        setActiveToolLog(null);
      }
    }
  }

  async function ensureSession(): Promise<AgentSession | null> {
    const client = clientRef.current;
    if (!client) {
      setBannerMessage("Connect to a sidecar first.");
      return null;
    }
    if (currentSessionRef.current) {
      return currentSessionRef.current;
    }
    const created = await client.createSession();
    upsertSession(created);
    setSelectedSessionId(created.id);
    setCurrentSession(created);
    return created;
  }

  async function selectSession(sessionId: string, client = clientRef.current, knownSessions?: AgentSession[]) {
    if (!client) {
      return;
    }
    const loadedSession = await client.loadSession(sessionId);
    setSelectedSessionId(sessionId);
    setCurrentSession(loadedSession);
    setStreamingText("");
    if (knownSessions) {
      setSessions(sortSessions(knownSessions.map((session) => (session.id === loadedSession.id ? loadedSession : session))));
    } else {
      upsertSession(loadedSession);
    }
  }

  function upsertSession(session: AgentSession) {
    setSessions((previous) => {
      const others = previous.filter((item) => item.id !== session.id);
      return sortSessions([session, ...others]);
    });
  }

  async function handleCreateSession() {
    const client = clientRef.current;
    if (!client) {
      setBannerMessage("Connect to a sidecar before creating a session.");
      return;
    }
    setBusyAction("create-session");
    try {
      const session = await client.createSession();
      upsertSession(session);
      setSelectedSessionId(session.id);
      setCurrentSession(session);
      setDraft("");
      setStreamingText("");
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSendPrompt() {
    const client = clientRef.current;
    if (!client || !draft.trim()) {
      return;
    }
    setBusyAction("send-prompt");
    try {
      const session = await ensureSession();
      if (!session) {
        return;
      }
      const prompt = draft;
      setDraft("");
      setStreamingText("");
      const response = await client.startTurn(session.id, prompt);
      setActiveTurnId(response.turn_id);
      setBannerMessage("Turn started.");
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleInterrupt() {
    const client = clientRef.current;
    if (!client || !activeTurnId) {
      return;
    }
    setBusyAction("interrupt-turn");
    try {
      await client.interruptTurn(activeTurnId);
      setBannerMessage("Interrupt requested.");
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleProviderChange(nextProvider: string) {
    setSelectedProvider(nextProvider);
    try {
      await refreshModels(nextProvider);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleApplyProviderModel() {
    const client = clientRef.current;
    if (!client || !selectedProvider || !selectedModel) {
      return;
    }
    setBusyAction("switch-provider");
    try {
      const result = await client.switchProviderModel(selectedProvider, selectedModel);
      setBannerMessage(result.message);
      await refreshStatusAndProviders();
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSelectToolLog(logId: string) {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    setInspectorTab("logs");
    setBusyAction("load-tool-log");
    try {
      setActiveToolLog(await client.getToolLog(logId));
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleResolveAuthorization(
    interactionId: string,
    scope: "once" | "workspace" | "deny",
    approved: boolean,
    reason: string,
  ) {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    setBusyAction("resolve-authorization");
    try {
      await client.resolveAuthorization(interactionId, { scope, approved, reason });
      await refreshInteractions();
      await refreshStatusAndProviders();
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleResolveModeSwitch(interaction: InteractionRequestState, approved: boolean) {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    const targetMode = typeof interaction.payload.target_mode === "string" ? interaction.payload.target_mode : undefined;
    const currentMode = typeof interaction.payload.current_mode === "string" ? interaction.payload.current_mode : status?.execution_mode ?? undefined;
    setBusyAction("resolve-mode-switch");
    try {
      await client.resolveModeSwitch(interaction.id, {
        approved,
        activeMode: approved ? targetMode : currentMode,
        reason: approved ? "Switched from the desktop UI." : "Stayed in the current mode.",
      });
      await refreshInteractions();
      await refreshStatusAndProviders();
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  const conversationRows = buildConversationRows(currentSession, streamingText);
  const firstPendingInteraction = pendingInteractions[0] ?? null;
  const todos = currentSession?.todo_items ?? [];

  return (
    <div className="shell">
      <div className="ambient ambient-left" />
      <div className="ambient ambient-right" />
      <header className="topbar">
        <div>
          <p className="eyebrow">Somnia Desktop MVP</p>
          <h1>Sidecar-native desktop workspace</h1>
        </div>
        <div className="connection-strip">
          <label className="field sidecar-url">
            <span>Sidecar</span>
            <input
              value={baseUrlInput}
              onChange={(event) => setBaseUrlInput(event.target.value)}
              placeholder={DEFAULT_SIDECAR_URL}
            />
          </label>
          <button className="action secondary" onClick={() => void connectToSidecar()} disabled={busyAction !== null}>
            {connectionState === "connecting" ? "Connecting..." : "Reconnect"}
          </button>
        </div>
      </header>

      <main className="workspace">
        <aside className="panel session-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Resume history</p>
              <h2>Sessions</h2>
            </div>
            <button className="action primary" onClick={() => void handleCreateSession()} disabled={busyAction !== null}>
              New Session
            </button>
          </div>
          <div className="session-list">
            {sessions.length === 0 ? (
              <div className="empty-card">
                <p>No sessions yet.</p>
                <span>Create one here or send a prompt to auto-create the first session.</span>
              </div>
            ) : (
              sessions.map((session) => (
                <button
                  key={session.id}
                  className={`session-card ${selectedSessionId === session.id ? "selected" : ""}`}
                  onClick={() => void selectSession(session.id)}
                >
                  <div className="session-card-head">
                    <strong>{session.id}</strong>
                    <span>{formatRelativeTime(session.updated_at ?? session.created_at)}</span>
                  </div>
                  <p>{buildSessionPreview(session)}</p>
                  <div className="session-card-meta">
                    <span>{session.todo_items.filter((item) => item.status !== "completed").length} open todos</span>
                    <span>{session.messages.length} msgs</span>
                  </div>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className="panel conversation-panel">
          <div className="panel-header conversation-header">
            <div>
              <p className="panel-kicker">Dialogue</p>
              <h2>{selectedSessionId ?? "No session selected"}</h2>
            </div>
            <div className="status-cluster">
              <span className={`status-pill ${connectionState}`}>{connectionState}</span>
              <span className="mode-pill">{status?.execution_mode_title ?? "Execution mode unavailable"}</span>
              <button className="action danger" onClick={() => void handleInterrupt()} disabled={!activeTurnId || busyAction !== null}>
                Interrupt
              </button>
            </div>
          </div>

          <div className="toolbar">
            <div className="toolbar-group">
              <label className="field compact">
                <span>Provider</span>
                <select
                  value={selectedProvider}
                  onChange={(event) => void handleProviderChange(event.target.value)}
                  disabled={providers.length === 0 || busyAction !== null}
                >
                  {providers.map((provider) => (
                    <option key={provider.name} value={provider.name}>
                      {provider.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field compact">
                <span>Model</span>
                <select
                  value={selectedModel}
                  onChange={(event) => setSelectedModel(event.target.value)}
                  disabled={models.length === 0 || busyAction !== null}
                >
                  {models.map((model) => (
                    <option key={model.name} value={model.name}>
                      {model.name}
                    </option>
                  ))}
                </select>
              </label>
              <button className="action secondary" onClick={() => void handleApplyProviderModel()} disabled={!selectedModel || busyAction !== null}>
                Apply model
              </button>
            </div>
            <div className="toolbar-meta">
              <span>{status?.workspace_root ?? "workspace unavailable"}</span>
              <span>{status?.provider ? `${status.provider} / ${status.model}` : "runtime unavailable"}</span>
            </div>
          </div>

          <div className="conversation-body">
            {conversationRows.length === 0 ? (
              <div className="empty-conversation">
                <h3>Start a session</h3>
                <p>Connect to a sidecar, choose a session, then send a prompt. Streaming output lands here.</p>
              </div>
            ) : (
              conversationRows.map((row) => (
                <article key={row.id} className={`bubble ${row.role}`}>
                  <header>
                    <span>{row.role === "user" ? "User" : "Assistant"}</span>
                    {row.isStreaming ? <em>streaming</em> : null}
                  </header>
                  <pre>{row.text}</pre>
                </article>
              ))
            )}
          </div>

          <div className="composer">
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Ask Somnia to inspect, plan, or implement against the current workspace."
              rows={5}
            />
            <div className="composer-actions">
              <span>{bannerMessage}</span>
              <button className="action primary" onClick={() => void handleSendPrompt()} disabled={!draft.trim() || busyAction !== null}>
                Send
              </button>
            </div>
          </div>
        </section>

        <aside className="panel inspector-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Inspector</p>
              <h2>State surfaces</h2>
            </div>
          </div>

          <div className="tab-strip">
            <button className={inspectorTab === "todos" ? "selected" : ""} onClick={() => setInspectorTab("todos")}>
              Todos
            </button>
            <button className={inspectorTab === "logs" ? "selected" : ""} onClick={() => setInspectorTab("logs")}>
              Tool Logs
            </button>
            <button className={inspectorTab === "approvals" ? "selected" : ""} onClick={() => setInspectorTab("approvals")}>
              Approvals
            </button>
          </div>

          {inspectorTab === "todos" ? (
            <div className="inspector-section">
              <div className="fact-row">
                <span>Current mode</span>
                <strong>{status?.execution_mode_title ?? "unknown"}</strong>
              </div>
              <div className="fact-row">
                <span>Reasoning</span>
                <strong>{status?.reasoning_level ?? "auto"}</strong>
              </div>
              <div className="todo-list">
                {todos.length === 0 ? (
                  <div className="empty-card">
                    <p>No open todo data.</p>
                    <span>TodoWrite output from the runtime will show up here as soon as a turn updates it.</span>
                  </div>
                ) : (
                  todos.map((item, index) => (
                    <div key={`${String(item.content)}-${index}`} className={`todo-card ${String(item.status ?? "pending").toLowerCase()}`}>
                      <strong>{String(item.status ?? "pending")}</strong>
                      <p>{formatTodoLabel(item)}</p>
                    </div>
                  ))
                )}
              </div>
            </div>
          ) : null}

          {inspectorTab === "logs" ? (
            <div className="inspector-section logs-layout">
              <div className="log-list">
                {toolLogs.length === 0 ? (
                  <div className="empty-card">
                    <p>No tool logs yet.</p>
                    <span>As soon as a turn calls tools, the recent log index will appear here.</span>
                  </div>
                ) : (
                  toolLogs.map((entry) => (
                    <button key={entry.id} className={`log-card ${activeToolLog?.id === entry.id ? "selected" : ""}`} onClick={() => void handleSelectToolLog(entry.id)}>
                      <div className="log-card-head">
                        <strong>{entry.tool_name}</strong>
                        <span>{formatRelativeTime(entry.timestamp)}</span>
                      </div>
                      <p>{entry.category} · {entry.actor}</p>
                    </button>
                  ))
                )}
              </div>
              <div className="log-detail">
                {activeToolLog ? (
                  <>
                    <div className="log-detail-head">
                      <h3>{activeToolLog.tool_name}</h3>
                      <span>{activeToolLog.id}</span>
                    </div>
                    <pre>{activeToolLog.rendered}</pre>
                  </>
                ) : (
                  <div className="empty-card">
                    <p>Select a tool log.</p>
                    <span>The detail view renders the same log text the CLI exposes through `/toollog`.</span>
                  </div>
                )}
              </div>
            </div>
          ) : null}

          {inspectorTab === "approvals" ? (
            <div className="inspector-section approvals-list">
              {pendingInteractions.length === 0 ? (
                <div className="empty-card">
                  <p>No pending approvals.</p>
                  <span>Authorization and mode-switch requests arrive here and also surface as modal prompts.</span>
                </div>
              ) : (
                pendingInteractions.map((interaction) => (
                  <div key={interaction.id} className="approval-card">
                    <header>
                      <strong>{interaction.kind === "authorization" ? "Authorization" : "Mode switch"}</strong>
                      <span>{interaction.id}</span>
                    </header>
                    <p>{interactionSummary(interaction)}</p>
                    {interaction.kind === "authorization" ? (
                      <div className="approval-actions">
                        <button onClick={() => void handleResolveAuthorization(interaction.id, "once", true, "Allowed once from desktop UI.")}>
                          Allow once
                        </button>
                        <button onClick={() => void handleResolveAuthorization(interaction.id, "workspace", true, "Allowed in this workspace from desktop UI.")}>
                          Allow workspace
                        </button>
                        <button className="danger-ghost" onClick={() => void handleResolveAuthorization(interaction.id, "deny", false, "Denied from desktop UI.")}>
                          Deny
                        </button>
                      </div>
                    ) : (
                      <div className="approval-actions">
                        <button onClick={() => void handleResolveModeSwitch(interaction, true)}>Switch</button>
                        <button className="danger-ghost" onClick={() => void handleResolveModeSwitch(interaction, false)}>Stay</button>
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          ) : null}
        </aside>
      </main>

      {firstPendingInteraction ? (
        <div className="modal-backdrop">
          <div className="modal">
            <p className="eyebrow">{firstPendingInteraction.kind === "authorization" ? "Authorization request" : "Mode switch request"}</p>
            <h2>{interactionTitle(firstPendingInteraction)}</h2>
            <p>{interactionSummary(firstPendingInteraction)}</p>
            {firstPendingInteraction.kind === "authorization" ? (
              <div className="modal-actions">
                <button className="action primary" onClick={() => void handleResolveAuthorization(firstPendingInteraction.id, "once", true, "Allowed once from desktop UI.")}>
                  Allow once
                </button>
                <button className="action secondary" onClick={() => void handleResolveAuthorization(firstPendingInteraction.id, "workspace", true, "Allowed in this workspace from desktop UI.")}>
                  Allow workspace
                </button>
                <button className="action danger" onClick={() => void handleResolveAuthorization(firstPendingInteraction.id, "deny", false, "Denied from desktop UI.")}>
                  Deny
                </button>
              </div>
            ) : (
              <div className="modal-actions">
                <button className="action primary" onClick={() => void handleResolveModeSwitch(firstPendingInteraction, true)}>
                  Switch now
                </button>
                <button className="action danger" onClick={() => void handleResolveModeSwitch(firstPendingInteraction, false)}>
                  Stay here
                </button>
              </div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function interactionTitle(interaction: InteractionRequestState): string {
  if (interaction.kind === "authorization") {
    const toolName = typeof interaction.payload.tool_name === "string" ? interaction.payload.tool_name : "tool";
    return `Approve ${toolName}`;
  }
  const targetMode = typeof interaction.payload.target_mode === "string" ? interaction.payload.target_mode : "another mode";
  return `Switch to ${targetMode}`;
}

function interactionSummary(interaction: InteractionRequestState): string {
  if (interaction.kind === "authorization") {
    const reason = typeof interaction.payload.reason === "string" ? interaction.payload.reason : "No reason provided.";
    const args = typeof interaction.payload.argument_summary === "string" ? interaction.payload.argument_summary : "";
    return args ? `${reason} Arguments: ${args}` : reason;
  }
  const reason = typeof interaction.payload.reason === "string" ? interaction.payload.reason : "No reason provided.";
  const currentMode = typeof interaction.payload.current_mode === "string" ? interaction.payload.current_mode : "unknown";
  const targetMode = typeof interaction.payload.target_mode === "string" ? interaction.payload.target_mode : "unknown";
  return `${reason} Requested transition: ${currentMode} -> ${targetMode}`;
}

function readSessionFromPayload(value: unknown): AgentSession | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const payload = value as AgentSession;
  if (typeof payload.id !== "string" || !Array.isArray(payload.messages)) {
    return null;
  }
  return payload;
}

function formatErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default App;
