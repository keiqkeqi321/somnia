import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ClipboardEvent as ReactClipboardEvent,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

import {
  chooseProjectFolder,
  ensureManagedSidecar,
  openWorkspaceRoot,
  stopManagedSidecar,
} from "./lib/desktop";
import { buildConversationRows, buildSessionPreview, formatRelativeTime, formatTodoLabel, sortSessions } from "./lib/messages";
import { SidecarClient, normalizeBaseUrl } from "./lib/sidecar";
import type {
  AgentSession,
  ConversationPendingTurn,
  InteractionRequestState,
  ManagedSidecarConnection,
  ModelDescriptor,
  ProviderDescriptor,
  SidecarEvent,
  SidecarStatus,
  TodoItem,
  ToolLogDetail,
  ToolLogIndexEntry,
} from "./types";

const STORAGE_KEY = "somnia.desktop.sidecar-url";
const PROJECTS_STORAGE_KEY = "somnia.desktop.project-paths";
const PROMPT_HISTORY_STORAGE_KEY = "somnia.desktop.prompt-history";
const LAYOUT_STORAGE_KEY = "somnia.desktop.layout";
const DEFAULT_SIDECAR_URL = "http://127.0.0.1:8765";
const TOOL_LIMIT = 24;
const SIDEBAR_MIN_WIDTH = 210;
const SIDEBAR_MAX_WIDTH = 430;
const CONTEXT_MIN_WIDTH = 280;
const CONTEXT_MAX_WIDTH = 540;
const CONVERSATION_MIN_WIDTH = 430;
const RESIZER_WIDTH = 10;
const REASONING_LEVEL_OPTIONS = ["auto", "low", "medium", "high", "deep"] as const;
const COMMAND_SPECS = [
  { command: "/scan", description: "Scan the repo or a subdirectory" },
  { command: "/symbols", description: "Find symbols and inspect matching source locations" },
  { command: "/image", description: "Send a local image to the active multimodal model" },
  { command: "/paste-image", description: "Read an image from the system clipboard" },
  { command: "/model", description: "Choose the active provider and model" },
  { command: "/reasoning", description: "Set the active provider reasoning level" },
  { command: "/providers", description: "Add or edit shared provider profiles" },
  { command: "/hooks", description: "Browse hooks by event and toggle them on or off" },
  { command: "/undo", description: "Undo the most recent file change set" },
  { command: "/checkpoint", description: "Save a named checkpoint of the current session state" },
  { command: "/rollback", description: "Roll back to a previous checkpoint" },
  { command: "/compact", description: "Compact the current session context" },
  { command: "/janitor", description: "Run semantic janitor on the current payload" },
  { command: "/skills", description: "Choose a skill to apply to the next prompt" },
  { command: "/tasks", description: "Show persistent tasks" },
  { command: "/team", description: "Show teammate roster and states" },
  { command: "/mcp", description: "Browse configured MCP servers and tools" },
  { command: "/bg", description: "Show background jobs" },
  { command: "/help", description: "Show available REPL commands" },
  { command: "/exit", description: "Exit chat mode" },
] as const;
const EXECUTION_MODE_OPTIONS = [
  { key: "shortcuts", title: "? for shortcuts", description: "Read-only shortcuts and lightweight inspection." },
  { key: "plan", title: "⏸ plan mode on", description: "Read-only planning before edits." },
  { key: "accept_edits", title: "⏵⏵ accept edits on", description: "Allow file edits and task updates." },
  { key: "yolo", title: "! Yolo", description: "Full autonomy for this workspace." },
] as const;

type ReasoningLevelOption = (typeof REASONING_LEVEL_OPTIONS)[number];
type ExecutionModeOption = (typeof EXECUTION_MODE_OPTIONS)[number]["key"];
type PendingImage = {
  id: string;
  name: string;
  mediaType: string;
  dataUrl: string;
};
type QueuedPrompt = {
  id: string;
  sessionId: string;
  prompt: string;
  images: PendingImage[];
  userText: string;
  injectionRequested?: boolean;
};
type ProjectState = {
  path: string;
  label: string;
  connection: ManagedSidecarConnection;
  status: SidecarStatus;
  sessions: AgentSession[];
  pendingInteractions: InteractionRequestState[];
  toolLogs: ToolLogIndexEntry[];
};
type TodoSummary = {
  visibleItems: TodoItem[];
  openItems: TodoItem[];
  completedCount: number;
  activeItem: TodoItem | null;
  nextItem: TodoItem | null;
};
type LayoutState = {
  sidebarWidth: number;
  contextWidth: number;
};
type LayoutDragState = {
  target: "sidebar" | "context";
  startX: number;
  startSidebarWidth: number;
  startContextWidth: number;
};
type ActiveProjectTurn = {
  sessionId: string;
  turnId: string | null;
};

const DEFAULT_CONVERSATION_PROJECT_KEY = "__default_project__";

function App() {
  const initialSavedUrl = typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEY) : null;
  const initialBaseUrl = normalizeBaseUrl(initialSavedUrl ?? DEFAULT_SIDECAR_URL);
  const [baseUrlInput, setBaseUrlInput] = useState(initialBaseUrl);
  const [connectionState, setConnectionState] = useState<"connecting" | "connected" | "disconnected" | "error">(
    "disconnected",
  );
  const [projects, setProjects] = useState<ProjectState[]>([]);
  const [selectedProjectPath, setSelectedProjectPath] = useState<string | null>(null);
  const [status, setStatus] = useState<SidecarStatus | null>(null);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [currentSession, setCurrentSession] = useState<AgentSession | null>(null);
  const [draft, setDraft] = useState("");
  const [streamingTexts, setStreamingTexts] = useState<Record<string, string>>({});
  const [pendingTurns, setPendingTurns] = useState<Record<string, ConversationPendingTurn>>({});
  const [queuedPrompts, setQueuedPrompts] = useState<Record<string, QueuedPrompt[]>>({});
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [activeProjectTurns, setActiveProjectTurns] = useState<Record<string, ActiveProjectTurn[]>>({});
  const [pendingInteractions, setPendingInteractions] = useState<InteractionRequestState[]>([]);
  const [providers, setProviders] = useState<ProviderDescriptor[]>([]);
  const [models, setModels] = useState<ModelDescriptor[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [selectedReasoningLevel, setSelectedReasoningLevel] = useState<ReasoningLevelOption>("auto");
  const [promptHistory, setPromptHistory] = useState<string[]>(() => readStoredPromptHistory());
  const [historyCursor, setHistoryCursor] = useState<number | null>(null);
  const [commandPickerOpen, setCommandPickerOpen] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);
  const [toolLogs, setToolLogs] = useState<ToolLogIndexEntry[]>([]);
  const [activeToolLog, setActiveToolLog] = useState<ToolLogDetail | null>(null);
  const [sidebarSection, setSidebarSection] = useState<"sessions">("sessions");
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [projectMenuOpenKey, setProjectMenuOpenKey] = useState<string | null>(null);
  const [contextPanelOpen, setContextPanelOpen] = useState(true);
  const [todoExpanded, setTodoExpanded] = useState(false);
  const [layout, setLayout] = useState<LayoutState>(() => readStoredLayout());
  const [layoutDragging, setLayoutDragging] = useState<LayoutDragState | null>(null);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modePickerOpen, setModePickerOpen] = useState(false);
  const [bannerMessage, setBannerMessage] = useState("Point the UI at a running sidecar and start a session.");
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const clientRef = useRef<SidecarClient | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const projectClientsRef = useRef<Record<string, SidecarClient>>({});
  const projectSocketsRef = useRef<Record<string, WebSocket>>({});
  const selectedProjectPathRef = useRef<string | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const currentSessionRef = useRef<AgentSession | null>(null);
  const queuedPromptsRef = useRef<Record<string, QueuedPrompt[]>>({});
  const workspaceRef = useRef<HTMLElement | null>(null);
  const modelPickerRef = useRef<HTMLDivElement | null>(null);
  const modePickerRef = useRef<HTMLDivElement | null>(null);
  const projectMenuRef = useRef<HTMLDivElement | null>(null);
  const composerTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  selectedSessionIdRef.current = selectedSessionId;
  selectedProjectPathRef.current = selectedProjectPath;
  currentSessionRef.current = currentSession;
  queuedPromptsRef.current = queuedPrompts;

  useEffect(() => {
    void initializeConnection();
    return () => {
      socketRef.current?.close();
      Object.values(projectSocketsRef.current).forEach((socket) => socket.close());
    };
    // Intentionally run only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!modelPickerOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (!modelPickerRef.current?.contains(event.target as Node)) {
        setModelPickerOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setModelPickerOpen(false);
      }
    }

    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [modelPickerOpen]);

  useEffect(() => {
    if (!modePickerOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (!modePickerRef.current?.contains(event.target as Node)) {
        setModePickerOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setModePickerOpen(false);
      }
    }

    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [modePickerOpen]);

  useEffect(() => {
    if (!projectMenuOpenKey) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (!projectMenuRef.current?.contains(event.target as Node)) {
        setProjectMenuOpenKey(null);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setProjectMenuOpenKey(null);
      }
    }

    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [projectMenuOpenKey]);

  useLayoutEffect(() => {
    resizeComposerTextarea();
  }, [draft]);

  useEffect(() => {
    const trimmed = draft.trimStart();
    const shouldOpen = /^\/[^\s]*$/.test(trimmed);
    setCommandPickerOpen(shouldOpen);
    setSelectedCommandIndex(0);
  }, [draft]);

  useEffect(() => {
    setTodoExpanded(false);
  }, [selectedSessionId]);

  useEffect(() => {
    if (!layoutDragging) {
      return;
    }

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    function handlePointerMove(event: PointerEvent) {
      updateLayoutDrag(event.clientX);
    }

    function handlePointerUp() {
      setLayoutDragging(null);
    }

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);
    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
    };
  }, [layoutDragging, contextPanelOpen]);

  useEffect(() => {
    function handleResize() {
      resizeComposerTextarea();
    }

    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
    };
  }, []);

  async function initializeConnection() {
    const savedUrl = typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEY) : null;
    const normalizedSavedUrl = savedUrl ? normalizeBaseUrl(savedUrl) : null;
    const shouldPreferManagedSidecar = normalizedSavedUrl === null || normalizedSavedUrl === DEFAULT_SIDECAR_URL;

    if (shouldPreferManagedSidecar) {
      try {
        const savedProjectPaths = readStoredProjectPaths();
        const managedConnection = await ensureManagedSidecar();
        if (managedConnection) {
          await connectManagedProject(managedConnection, { selectProject: true });
          for (const projectPath of savedProjectPaths) {
            if (projectPath === managedConnection.workspaceRoot) {
              continue;
            }
            try {
              const projectConnection = await ensureManagedSidecar(projectPath);
              if (projectConnection) {
                await connectManagedProject(projectConnection, { selectProject: false });
              }
            } catch (error) {
              setBannerMessage(`Unable to restore project '${projectPath}': ${formatErrorMessage(error)}`);
            }
          }
          return;
        }
      } catch (error) {
        await connectToSidecar(normalizedSavedUrl ?? DEFAULT_SIDECAR_URL, {
          errorPrefix: `Bundled sidecar unavailable: ${formatErrorMessage(error)}. `,
        });
        return;
      }
    }

    await connectToSidecar(normalizedSavedUrl ?? DEFAULT_SIDECAR_URL);
  }

  async function connectManagedProject(
    managedConnection: ManagedSidecarConnection,
    options: { selectProject: boolean } = { selectProject: true },
  ) {
    const client = new SidecarClient(managedConnection.baseUrl);
    setConnectionState("connecting");
    setBannerMessage(`Connecting to ${managedConnection.workspaceRoot}...`);

    const [runtimeStatus, sessionList, providerList, interactionList, logList] = await Promise.all([
      client.runtimeStatus(),
      client.listSessions(),
      client.listProviders(),
      client.listInteractions(),
      client.listToolLogs(TOOL_LIMIT),
    ]);
    const projectPath = runtimeStatus.workspace_root || managedConnection.workspaceRoot;
    const project: ProjectState = {
      path: projectPath,
      label: getPathLeafName(projectPath),
      connection: managedConnection,
      status: runtimeStatus,
      sessions: sortSessions(sessionList),
      pendingInteractions: interactionList,
      toolLogs: logList,
    };

    projectClientsRef.current[projectPath] = client;
    setProjects((previous) => upsertProject(previous, project));
    persistProjectPath(projectPath);
    openEventSocket(client, runtimeStatus.ws_url, projectPath);
    setConnectionState("connected");

    if (options.selectProject) {
      await activateProject(projectPath, client, project);
    }
  }

  async function activateProject(projectPath: string, client = projectClientsRef.current[projectPath], project?: ProjectState) {
    const nextProject = project ?? projects.find((item) => item.path === projectPath);
    if (!client || !nextProject) {
      return;
    }
    clientRef.current = client;
    socketRef.current = projectSocketsRef.current[projectPath] ?? null;
    setSelectedProjectPath(projectPath);
    setStatus(nextProject.status);
    setSessions(nextProject.sessions);
    setPendingInteractions(nextProject.pendingInteractions);
    setToolLogs(nextProject.toolLogs);
    setProviders(await client.listProviders());
    setSelectedProvider(nextProject.status.provider);
    setSelectedReasoningLevel(normalizeReasoningLevel(nextProject.status.reasoning_level));
    await refreshModels(nextProject.status.provider, client, nextProject.status.model);

    const nextSessionId =
      nextProject.sessions.find((session) => session.id === selectedSessionIdRef.current)?.id ?? nextProject.sessions[0]?.id ?? null;
    if (nextSessionId) {
      await selectSession(nextSessionId, client, nextProject.sessions, projectPath);
    } else {
      setSelectedSessionId(null);
      setCurrentSession(null);
    }
    setBannerMessage(`Active project: ${projectPath}`);
  }

  async function connectToSidecar(
    nextBaseUrl = baseUrlInput,
    options: {
      errorPrefix?: string;
      managedConnection?: ManagedSidecarConnection;
      persistBaseUrl?: boolean;
    } = {},
  ) {
    const { errorPrefix = "", managedConnection, persistBaseUrl = true } = options;
    const normalizedBaseUrl = normalizeBaseUrl(nextBaseUrl);
    setConnectionState("connecting");
    setBaseUrlInput(normalizedBaseUrl);
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
      setSelectedReasoningLevel(normalizeReasoningLevel(runtimeStatus.reasoning_level));
      setBannerMessage(
        managedConnection ? `Connected to bundled sidecar at ${runtimeStatus.base_url}` : `Connected to ${runtimeStatus.base_url}`,
      );
      if (persistBaseUrl && typeof window !== "undefined") {
        window.localStorage.setItem(STORAGE_KEY, normalizedBaseUrl);
      }

      const sortedSessions = sortSessions(sessionList);
      const projectPath = runtimeStatus.workspace_root;
      const project: ProjectState = {
        path: projectPath,
        label: getPathLeafName(projectPath),
        connection: managedConnection ?? {
          baseUrl: normalizedBaseUrl,
          wsUrl: runtimeStatus.ws_url,
          workspaceRoot: projectPath,
        },
        status: runtimeStatus,
        sessions: sortedSessions,
        pendingInteractions: interactionList,
        toolLogs: logList,
      };
      projectClientsRef.current[projectPath] = nextClient;
      setProjects((previous) => upsertProject(previous, project));
      setSelectedProjectPath(projectPath);
      setSessions(sortedSessions);
      const nextSessionId =
        sortedSessions.find((session) => session.id === selectedSessionIdRef.current)?.id ?? sortedSessions[0]?.id ?? null;
      if (nextSessionId) {
        await selectSession(nextSessionId, nextClient, sortedSessions, projectPath);
      } else {
        setSelectedSessionId(null);
        setCurrentSession(null);
      }
      await refreshModels(runtimeStatus.provider, nextClient, runtimeStatus.model);
      openEventSocket(nextClient, runtimeStatus.ws_url, runtimeStatus.workspace_root);
    } catch (error) {
      clientRef.current = null;
      setConnectionState("error");
      setBannerMessage(`${errorPrefix}${formatErrorMessage(error)}`);
      setStatus(null);
    }
  }

  function openEventSocket(client: SidecarClient, wsUrl: string, projectPath: string) {
    projectSocketsRef.current[projectPath]?.close();
    const socket = client.createEventSocket(wsUrl);
    projectSocketsRef.current[projectPath] = socket;
    if (selectedProjectPathRef.current === projectPath || !selectedProjectPathRef.current) {
      socketRef.current = socket;
    }

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
        void handleSidecarEvent(projectPath, event);
      } catch (error) {
        setBannerMessage(`Ignored malformed sidecar event: ${formatErrorMessage(error)}`);
      }
    };
  }

  function conversationStateKey(projectPath: string | null | undefined, sessionId: string | null | undefined): string | null {
    const project = String(projectPath ?? DEFAULT_CONVERSATION_PROJECT_KEY).trim();
    const sessionIdValue = String(sessionId ?? "").trim();
    return project && sessionIdValue ? `${project}\n${sessionIdValue}` : null;
  }

  function clearConversationRuntimeState(projectPath: string | null | undefined, sessionId: string | null | undefined) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    setStreamingTexts((previous) => {
      if (!(key in previous)) {
        return previous;
      }
      const next = { ...previous };
      delete next[key];
      return next;
    });
    setPendingTurns((previous) => {
      if (!(key in previous)) {
        return previous;
      }
      const next = { ...previous };
      delete next[key];
      return next;
    });
  }

  function enqueueSessionPrompt(
    projectPath: string | null | undefined,
    sessionId: string,
    prompt: string,
    images: PendingImage[],
  ) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    const item: QueuedPrompt = {
      id: `queued-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      sessionId,
      prompt,
      images: images.map((image) => ({ ...image })),
      userText: buildOptimisticUserText(prompt, images),
    };
    setQueuedPrompts((previous) => ({
      ...previous,
      [key]: [...(previous[key] ?? []), item],
    }));
  }

  function takeNextQueuedPrompt(projectPath: string | null | undefined, sessionId: string): QueuedPrompt | null {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return null;
    }
    const current = queuedPromptsRef.current[key] ?? [];
    const [nextItem, ...remaining] = current;
    if (!nextItem) {
      return null;
    }
    const nextState = { ...queuedPromptsRef.current };
    if (remaining.length > 0) {
      nextState[key] = remaining;
    } else {
      delete nextState[key];
    }
    queuedPromptsRef.current = nextState;
    setQueuedPrompts(nextState);
    return nextItem;
  }

  function updateQueuedPrompt(
    projectPath: string | null | undefined,
    sessionId: string,
    promptId: string,
    updater: (prompt: QueuedPrompt) => QueuedPrompt,
  ) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    setQueuedPrompts((previous) => {
      const current = previous[key] ?? [];
      if (!current.some((prompt) => prompt.id === promptId)) {
        return previous;
      }
      return {
        ...previous,
        [key]: current.map((prompt) => (prompt.id === promptId ? updater(prompt) : prompt)),
      };
    });
  }

  function removeQueuedPrompt(projectPath: string | null | undefined, sessionId: string, promptId: string) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    setQueuedPrompts((previous) => {
      const current = previous[key] ?? [];
      const remaining = current.filter((prompt) => prompt.id !== promptId);
      if (remaining.length === current.length) {
        return previous;
      }
      const next = { ...previous };
      if (remaining.length > 0) {
        next[key] = remaining;
      } else {
        delete next[key];
      }
      queuedPromptsRef.current = next;
      return next;
    });
  }

  async function handleSidecarEvent(projectPath: string, event: SidecarEvent) {
    const isActiveProject = selectedProjectPathRef.current === projectPath;
    if (event.type === "sidecar_ready") {
      return;
    }
    if (event.type === "session_created") {
      const payloadSession = readSessionFromPayload(event.payload.session);
      if (payloadSession) {
        upsertProjectSession(projectPath, payloadSession);
        if (isActiveProject) {
          setSelectedSessionId(payloadSession.id);
          setCurrentSession(payloadSession);
        }
      }
      return;
    }
    if (event.type === "turn_started") {
      if (event.session_id) {
        setActiveProjectTurns((previous) => ({
          ...previous,
          [projectPath]: [
            ...(previous[projectPath] ?? []).filter((turn) => turn.turnId !== event.turn_id && turn.sessionId !== event.session_id),
            {
              sessionId: event.session_id ?? "",
              turnId: event.turn_id ?? null,
            },
          ].slice(-2),
        }));
      }
      if (isActiveProject && event.session_id && event.session_id === selectedSessionIdRef.current) {
        setActiveTurnId(event.turn_id ?? null);
      }
      return;
    }
    if (event.type === "assistant_delta") {
      const key = conversationStateKey(projectPath, event.session_id);
      if (!key) {
        return;
      }
      const delta = typeof event.payload.delta === "string" ? event.payload.delta : "";
      if (delta) {
        setStreamingTexts((previous) => ({ ...previous, [key]: `${previous[key] ?? ""}${delta}` }));
      }
      return;
    }
    if (event.type === "session_updated") {
      const payloadSession = readSessionFromPayload(event.payload.session);
      if (payloadSession) {
        upsertProjectSession(projectPath, payloadSession);
        if (isActiveProject && payloadSession.id === selectedSessionIdRef.current) {
          setCurrentSession(payloadSession);
        }
      }
      return;
    }
    if (event.type === "todo_updated") {
      const items = Array.isArray(event.payload.items) ? event.payload.items : null;
      const session = currentSessionRef.current;
      if (!isActiveProject || !items || !session || event.session_id !== session.id) {
        return;
      }
      const nextSession = { ...session, todo_items: items } as AgentSession;
      setCurrentSession(nextSession);
      upsertProjectSession(projectPath, nextSession);
      return;
    }
    if (event.type === "loop_user_message_injected") {
      const injectionId = typeof event.payload.injection_id === "string" ? event.payload.injection_id : "";
      if (event.session_id && injectionId) {
        removeQueuedPrompt(projectPath, event.session_id, injectionId);
      }
      return;
    }
    if (event.type === "tool_finished") {
      if (isActiveProject) {
        void refreshToolLogs();
      }
      return;
    }
    if (event.type === "provider_switched" || event.type === "reasoning_level_updated" || event.type === "execution_mode_updated") {
      if (isActiveProject) {
        void refreshStatusAndProviders();
      }
      return;
    }
    if (event.type === "authorization_requested" || event.type === "mode_switch_requested") {
      if (isActiveProject) {
        void refreshInteractions();
      }
      return;
    }
    if (event.type === "interrupt_completed" || event.type === "error") {
      clearActiveProjectTurn(projectPath, event.turn_id ?? null);
      clearConversationRuntimeState(projectPath, event.session_id);
      if (isActiveProject) {
        if (event.session_id === selectedSessionIdRef.current) {
          setActiveTurnId((current) => (current === event.turn_id ? null : current));
        }
        void refreshInteractions();
        void refreshStatusAndProviders();
      }
      return;
    }
    if (event.type === "turn_result") {
      clearActiveProjectTurn(projectPath, event.turn_id ?? null);
      clearConversationRuntimeState(projectPath, event.session_id);
      const completedSessionId = event.session_id ?? null;
      if (isActiveProject) {
        if (completedSessionId === selectedSessionIdRef.current) {
          setActiveTurnId((current) => (current === event.turn_id ? null : current));
        }
      }
      const payloadSession = readSessionFromPayload(event.payload.session);
      if (payloadSession) {
        clearConversationRuntimeState(projectPath, payloadSession.id);
        upsertProjectSession(projectPath, payloadSession);
        if (isActiveProject && payloadSession.id === selectedSessionIdRef.current) {
          setCurrentSession(payloadSession);
        }
      }
      if (isActiveProject) {
        void refreshInteractions();
        void refreshToolLogs();
        void refreshStatusAndProviders();
      }
      if (completedSessionId) {
        void startNextQueuedPrompt(projectPath, completedSessionId);
      }
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
    setSelectedReasoningLevel(normalizeReasoningLevel(runtimeStatus.reasoning_level));
    updateActiveProject({ status: runtimeStatus, pendingInteractions: interactionList });
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
    const interactionList = await client.listInteractions();
    setPendingInteractions(interactionList);
    updateActiveProject({ pendingInteractions: interactionList });
  }

  async function refreshToolLogs() {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    const nextLogs = await client.listToolLogs(TOOL_LIMIT);
    setToolLogs(nextLogs);
    updateActiveProject({ toolLogs: nextLogs });
    if (activeToolLog) {
      try {
        setActiveToolLog(await client.getToolLog(activeToolLog.id));
      } catch {
        setActiveToolLog(null);
      }
    }
  }

  async function ensureSession(
    client = clientRef.current,
    projectPath = selectedProjectPathRef.current,
  ): Promise<AgentSession | null> {
    if (!client) {
      setBannerMessage("Connect to a sidecar first.");
      return null;
    }
    const selectedProjectMatches = projectPath === selectedProjectPathRef.current;
    if (selectedProjectMatches && currentSessionRef.current) {
      return currentSessionRef.current;
    }
    const created = await client.createSession();
    upsertProjectSession(projectPath, created);
    if (selectedProjectMatches) {
      setSelectedSessionId(created.id);
      setCurrentSession(created);
      setSidebarSection("sessions");
    }
    return created;
  }

  async function selectSession(
    sessionId: string,
    client = clientRef.current,
    knownSessions?: AgentSession[],
    projectPath = selectedProjectPathRef.current,
  ) {
    if (!client) {
      return;
    }
    const loadedSession = await client.loadSession(sessionId);
    setSelectedSessionId(sessionId);
    setCurrentSession(loadedSession);
    setActiveTurnId(
      projectPath ? (activeProjectTurns[projectPath] ?? []).find((turn) => turn.sessionId === sessionId)?.turnId ?? null : null,
    );
    if (knownSessions) {
      setSessions(sortSessions(knownSessions.map((session) => (session.id === loadedSession.id ? loadedSession : session))));
    } else {
      upsertProjectSession(projectPath, loadedSession);
    }
  }

  function upsertProjectSession(projectPath: string | null, session: AgentSession) {
    setSessions((previous) => {
      const others = previous.filter((item) => item.id !== session.id);
      return sortSessions([session, ...others]);
    });
    if (!projectPath) {
      return;
    }
    setProjects((previous) =>
      previous.map((project) => {
        if (project.path !== projectPath) {
          return project;
        }
        const others = project.sessions.filter((item) => item.id !== session.id);
        return { ...project, sessions: sortSessions([session, ...others]) };
      }),
    );
  }

  function updateActiveProject(patch: Partial<Pick<ProjectState, "status" | "pendingInteractions" | "toolLogs" | "sessions">>) {
    const projectPath = selectedProjectPathRef.current;
    if (!projectPath) {
      return;
    }
    setProjects((previous) => previous.map((project) => (project.path === projectPath ? { ...project, ...patch } : project)));
  }

  function clearActiveProjectTurn(projectPath: string, turnId: string | null) {
    setActiveProjectTurns((previous) => {
      const current = previous[projectPath] ?? [];
      const remaining = turnId ? current.filter((turn) => turn.turnId !== turnId) : [];
      if (remaining.length === current.length) {
        return previous;
      }
      const next = { ...previous };
      if (remaining.length > 0) {
        next[projectPath] = remaining;
      } else {
        delete next[projectPath];
      }
      return next;
    });
  }

  async function handleCreateProject() {
    setBusyAction("create-project");
    try {
      const projectPath = await chooseProjectFolder();
      if (!projectPath) {
        setBannerMessage("No project folder selected.");
        return;
      }
      const managedConnection = await ensureManagedSidecar(projectPath);
      if (!managedConnection) {
        setBannerMessage("Project folder selection is only available in the desktop app.");
        return;
      }
      await connectManagedProject(managedConnection, { selectProject: true });
      setContextPanelOpen(true);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleCreateSession(projectPath = selectedProjectPathRef.current) {
    const client = projectPath ? projectClientsRef.current[projectPath] ?? clientRef.current : clientRef.current;
    if (!client) {
      setBannerMessage("Connect to a sidecar before creating a session.");
      return;
    }
    setBusyAction("create-session");
    try {
      if (projectPath && projectPath !== selectedProjectPathRef.current) {
        await activateProject(projectPath, client);
      }
      const session = await client.createSession();
      upsertProjectSession(projectPath, session);
      setSelectedSessionId(session.id);
      setCurrentSession(session);
      setSidebarSection("sessions");
      setContextPanelOpen(true);
      setDraft("");
      clearConversationRuntimeState(projectPath, session.id);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleRemoveProject(projectPath: string) {
    const project = projects.find((item) => item.path === projectPath);
    if (!project) {
      return;
    }
    setBusyAction("remove-project");
    try {
      projectSocketsRef.current[projectPath]?.close();
      delete projectSocketsRef.current[projectPath];
      delete projectClientsRef.current[projectPath];
      await stopManagedSidecar(projectPath);
      removeStoredProjectPath(projectPath);
      setActiveProjectTurns((previous) => {
        const next = { ...previous };
        delete next[projectPath];
        return next;
      });

      const remainingProjects = projects.filter((item) => item.path !== projectPath);
      setProjects(remainingProjects);
      setCollapsedProjects((previous) => {
        const next = { ...previous };
        delete next[projectPath];
        return next;
      });
      setProjectMenuOpenKey(null);

      if (selectedProjectPathRef.current === projectPath) {
        const nextProject = remainingProjects[0] ?? null;
        if (nextProject) {
          await activateProject(nextProject.path, projectClientsRef.current[nextProject.path], nextProject);
        } else {
          clientRef.current = null;
          socketRef.current = null;
          setSelectedProjectPath(null);
          setStatus(null);
          setSessions([]);
          setSelectedSessionId(null);
          setCurrentSession(null);
          setPendingInteractions([]);
          setToolLogs([]);
          setActiveTurnId(null);
        }
      }
      setBannerMessage(`Removed project: ${project.label}`);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function startPromptTurn(
    client: SidecarClient,
    projectPath: string | null,
    session: AgentSession,
    prompt: string,
    images: PendingImage[],
  ) {
    const optimisticTurnId = `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const key = conversationStateKey(projectPath, session.id);
    const optimisticUserText = buildOptimisticUserText(prompt, images);
    if (key) {
      setPendingTurns((previous) => ({
        ...previous,
        [key]: {
          id: optimisticTurnId,
          sessionId: session.id,
          userText: optimisticUserText,
          placeholderText: "Thinking",
        },
      }));
      setStreamingTexts((previous) => ({ ...previous, [key]: "" }));
    }
    const userInput = buildPromptPayload(prompt, images);
    const response = await client.startTurn(session.id, userInput);
    if (key) {
      setPendingTurns((previous) => {
        const current = previous[key];
        if (!current || current.id !== optimisticTurnId) {
          return previous;
        }
        return { ...previous, [key]: { ...current, id: response.turn_id, sessionId: session.id } };
      });
    }
    if (projectPath === selectedProjectPathRef.current && session.id === selectedSessionIdRef.current) {
      setActiveTurnId(response.turn_id);
    }
    return response;
  }

  async function startNextQueuedPrompt(projectPath: string | null, sessionId: string) {
    const client = projectPath ? projectClientsRef.current[projectPath] ?? clientRef.current : clientRef.current;
    if (!client) {
      return;
    }
    const nextPrompt = takeNextQueuedPrompt(projectPath, sessionId);
    if (!nextPrompt) {
      return;
    }
    try {
      const session = await client.loadSession(sessionId);
      upsertProjectSession(projectPath, session);
      if (projectPath === selectedProjectPathRef.current && session.id === selectedSessionIdRef.current) {
        setCurrentSession(session);
      }
      await startPromptTurn(client, projectPath, session, nextPrompt.prompt, nextPrompt.images);
      setBannerMessage("Queued prompt started.");
    } catch (error) {
      enqueueSessionPrompt(projectPath, sessionId, nextPrompt.prompt, nextPrompt.images);
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleQueuePromptInjection(prompt: QueuedPrompt) {
    const projectPath = selectedProjectPathRef.current;
    const client = clientRef.current;
    const activeTurn = projectPath
      ? (activeProjectTurns[projectPath] ?? []).find((turn) => turn.sessionId === prompt.sessionId)
      : null;
    if (!client || !projectPath || !activeTurn?.turnId) {
      setBannerMessage("No active turn is available for loop injection.");
      return;
    }
    updateQueuedPrompt(projectPath, prompt.sessionId, prompt.id, (current) => ({ ...current, injectionRequested: true }));
    try {
      await client.queueLoopInjection(activeTurn.turnId, prompt.id, buildPromptPayload(prompt.prompt, prompt.images));
      setBannerMessage("Queued prompt will be injected on the next agent loop.");
    } catch (error) {
      updateQueuedPrompt(projectPath, prompt.sessionId, prompt.id, (current) => ({ ...current, injectionRequested: false }));
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleSendPrompt() {
    const client = clientRef.current;
    if (!client || (!draft.trim() && pendingImages.length === 0)) {
      return;
    }
    const projectPath = selectedProjectPathRef.current;
    const activeProjectTurnList = projectPath ? (activeProjectTurns[projectPath] ?? []) : [];
    const currentSessionId = currentSessionRef.current?.id ?? null;
    const prompt = draft;
    const images = pendingImages;
    if (currentSessionId && activeProjectTurnList.some((turn) => turn.sessionId === currentSessionId)) {
      enqueueSessionPrompt(projectPath, currentSessionId, prompt, images);
      rememberPrompt(prompt);
      setDraft("");
      setPendingImages([]);
      setHistoryCursor(null);
      setCommandPickerOpen(false);
      setBannerMessage("Prompt queued for this session.");
      return;
    }
    if (activeProjectTurnList.length >= 2) {
      setBannerMessage("This project already has two sessions running. Wait for one to finish before starting another turn.");
      return;
    }
    setBusyAction("send-prompt");
    let promptSessionId: string | null = null;
    try {
      const session = await ensureSession(client, projectPath);
      if (!session) {
        return;
      }
      promptSessionId = session.id;
      rememberPrompt(prompt);
      setDraft("");
      setPendingImages([]);
      setHistoryCursor(null);
      setCommandPickerOpen(false);
      await startPromptTurn(client, projectPath, session, prompt, images);
      setBannerMessage("Turn started.");
    } catch (error) {
      clearConversationRuntimeState(projectPath, promptSessionId);
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleInterrupt() {
    const client = clientRef.current;
    const selectedTurnId = selectedProjectPathRef.current
      ? (activeProjectTurns[selectedProjectPathRef.current] ?? []).find((turn) => turn.sessionId === selectedSessionIdRef.current)?.turnId
      : null;
    if (!client || !selectedTurnId) {
      return;
    }
    setBusyAction("interrupt-turn");
    try {
      await client.interruptTurn(selectedTurnId);
      setBannerMessage("Interrupt requested.");
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleProviderChange(nextProvider: string) {
    setSelectedProvider(nextProvider);
    setSelectedReasoningLevel(normalizeReasoningLevel(providers.find((provider) => provider.name === nextProvider)?.reasoning_level));
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
      await client.switchProviderModel(selectedProvider, selectedModel);
      await client.setReasoningLevel(selectedReasoningLevel === "auto" ? null : selectedReasoningLevel);
      await refreshStatusAndProviders();
      setModelPickerOpen(false);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  function rememberPrompt(prompt: string) {
    const normalized = prompt.trim();
    if (!normalized) {
      return;
    }
    setPromptHistory((previous) => {
      const deduped = previous.filter((item) => item !== normalized);
      const next = [...deduped, normalized].slice(-100);
      persistPromptHistory(next);
      return next;
    });
  }

  function handleComposerChange(value: string) {
    setDraft(value);
    setHistoryCursor(null);
  }

  async function handleComposerPaste(event: ReactClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    try {
      const images = await Promise.all(files.map((file) => readClipboardImage(file)));
      setPendingImages((previous) => [...previous, ...images].slice(-8));
      if (!draft.trim()) {
        setDraft("Look at this image.");
      }
      setCommandPickerOpen(false);
    } catch (error) {
      setBannerMessage(`Unable to read pasted image: ${formatErrorMessage(error)}`);
    }
  }

  function removePendingImage(imageId: string) {
    setPendingImages((previous) => previous.filter((image) => image.id !== imageId));
  }

  async function handleImageFilesSelected(fileList: FileList | null) {
    const files = Array.from(fileList ?? []).filter((file) => file.type.startsWith("image/"));
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
    if (files.length === 0) {
      return;
    }
    try {
      const images = await Promise.all(files.map((file) => readClipboardImage(file)));
      setPendingImages((previous) => [...previous, ...images].slice(-8));
      if (!draft.trim()) {
        setDraft("Look at this image.");
      }
      setCommandPickerOpen(false);
      composerTextareaRef.current?.focus();
    } catch (error) {
      setBannerMessage(`Unable to read selected image: ${formatErrorMessage(error)}`);
    }
  }

  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    const suggestions = currentCommandSuggestions(draft);
    if (commandPickerOpen && suggestions.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSelectedCommandIndex((current) => (current + 1) % suggestions.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSelectedCommandIndex((current) => (current - 1 + suggestions.length) % suggestions.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        applyCommandSuggestion(suggestions[selectedCommandIndex]?.command ?? suggestions[0].command);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setCommandPickerOpen(false);
        return;
      }
    }

    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void handleSendPrompt();
      return;
    }

    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") {
      return;
    }
    if (promptHistory.length === 0) {
      return;
    }
    const textarea = event.currentTarget;
    const atStart = textarea.selectionStart === 0 && textarea.selectionEnd === 0;
    const atEnd = textarea.selectionStart === draft.length && textarea.selectionEnd === draft.length;
    if (event.key === "ArrowUp" && !atStart) {
      return;
    }
    if (event.key === "ArrowDown" && !atEnd) {
      return;
    }
    event.preventDefault();
    const nextCursor =
      event.key === "ArrowUp"
        ? historyCursor === null
          ? promptHistory.length - 1
          : Math.max(0, historyCursor - 1)
        : historyCursor === null
          ? null
          : historyCursor >= promptHistory.length - 1
            ? null
            : historyCursor + 1;
    setHistoryCursor(nextCursor);
    setDraft(nextCursor === null ? "" : promptHistory[nextCursor]);
  }

  function beginLayoutDrag(target: LayoutDragState["target"], event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    setLayoutDragging({
      target,
      startX: event.clientX,
      startSidebarWidth: layout.sidebarWidth,
      startContextWidth: layout.contextWidth,
    });
  }

  function updateLayoutDrag(clientX: number) {
    setLayoutDragging((drag) => {
      if (!drag) {
        return null;
      }
      const containerWidth = workspaceRef.current?.getBoundingClientRect().width ?? 0;
      if (containerWidth <= 0) {
        return drag;
      }
      const nextLayout = nextDraggedLayout(drag, clientX, containerWidth, contextPanelOpen);
      setLayout(nextLayout);
      persistLayout(nextLayout);
      return drag;
    });
  }

  function applyCommandSuggestion(command: string) {
    setDraft(`${command} `);
    setCommandPickerOpen(false);
    requestAnimationFrame(() => {
      composerTextareaRef.current?.focus();
    });
  }

  async function handleExecutionModeChange(mode: ExecutionModeOption) {
    const client = clientRef.current;
    if (!client) {
      setBannerMessage("Connect to a sidecar before changing execution mode.");
      return;
    }
    setBusyAction("switch-execution-mode");
    try {
      await client.setExecutionMode(mode);
      setModePickerOpen(false);
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
    setSidebarSection("sessions");
    setContextPanelOpen(true);
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
    const currentMode = typeof interaction.payload.current_mode === "string" ? interaction.payload.current_mode : "unknown";
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

  function resizeComposerTextarea() {
    const textarea = composerTextareaRef.current;
    if (!textarea) {
      return;
    }

    const computedStyle = window.getComputedStyle(textarea);
    const lineHeight = Number.parseFloat(computedStyle.lineHeight) || 24;
    const paddingTop = Number.parseFloat(computedStyle.paddingTop) || 0;
    const paddingBottom = Number.parseFloat(computedStyle.paddingBottom) || 0;
    const borderTop = Number.parseFloat(computedStyle.borderTopWidth) || 0;
    const borderBottom = Number.parseFloat(computedStyle.borderBottomWidth) || 0;
    const chromeHeight = paddingTop + paddingBottom + borderTop + borderBottom;
    const minHeight = lineHeight + chromeHeight;
    const maxHeight = lineHeight * 10 + chromeHeight;

    textarea.style.height = "auto";
    const contentHeight = textarea.scrollHeight + borderTop + borderBottom;
    const nextHeight = Math.max(minHeight, Math.min(contentHeight, maxHeight));
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
  }

  const activeConversationKey = conversationStateKey(selectedProjectPath, currentSession?.id);
  const activePendingTurn = activeConversationKey ? pendingTurns[activeConversationKey] ?? null : null;
  const activeStreamingText = activeConversationKey ? streamingTexts[activeConversationKey] ?? "" : "";
  const activeQueuedPrompts = activeConversationKey ? queuedPrompts[activeConversationKey] ?? [] : [];
  const conversationRows = buildConversationRows(currentSession, activeStreamingText, activePendingTurn);
  const currentSessionInteraction = currentSession ? findSessionInteraction(pendingInteractions, currentSession.id) : null;
  const activeProjectTurnList = selectedProjectPath ? (activeProjectTurns[selectedProjectPath] ?? []) : [];
  const currentSessionTurn = currentSession ? activeProjectTurnList.find((turn) => turn.sessionId === currentSession.id) ?? null : null;
  const currentSessionRunning = currentSession ? activeProjectTurnList.some((turn) => turn.sessionId === currentSession.id) : false;
  const projectTurnLimitReached = activeProjectTurnList.length >= 2;
  const activeProviderLabel = status?.provider ?? selectedProvider ?? "Provider";
  const activeModelLabel = status?.model ?? selectedModel ?? "Model";
  const activeReasoningLabel = formatReasoningLevel(status?.reasoning_level ?? selectedReasoningLevel);
  const activeExecutionMode = normalizeExecutionMode(status?.execution_mode);
  const activeExecutionModeLabel =
    status?.execution_mode_title ?? EXECUTION_MODE_OPTIONS.find((mode) => mode.key === activeExecutionMode)?.title ?? "Execution mode unavailable";
  const contextUsage = currentSession?.context_window_usage ?? null;
  const contextPercent = normalizeContextPercent(contextUsage?.usage_percent);
  const contextColor = contextUsageColor(contextPercent);
  const contextFill = contextPercent ?? 0;
  const contextLabel = contextUsage
    ? contextUsage.max_tokens
      ? `CTX ${contextPercent?.toFixed(1) ?? "0.0"}%`
      : `CTX ${formatTokenCount(contextUsage.used_tokens)}`
    : "CTX --";
  const contextTitle = contextUsage
    ? contextUsage.max_tokens
      ? `Context: ${contextPercent?.toFixed(1) ?? "0.0"}% (${formatTokenCount(contextUsage.used_tokens)} / ${formatTokenCount(
          contextUsage.max_tokens,
        )} tokens)`
      : `Context: ${formatTokenCount(contextUsage.used_tokens)} tokens`
    : "Context usage unavailable";
  const commandSuggestions = currentCommandSuggestions(draft);
  const conversationPreview = currentSession ? buildSessionPreview(currentSession) : "";
  const conversationTitle = truncateTopic(conversationPreview || selectedSessionId || "New conversation");
  const todoSummary = currentSession ? buildTodoSummary(currentSession.todo_items) : null;
  const workspaceRootPath = status?.workspace_root ?? "";
  const workspaceRootName = workspaceRootPath ? getPathLeafName(workspaceRootPath) : "workspace";
  const sessionProjectGroups = projects.map((project) => ({
    key: project.path,
    label: project.label,
    path: project.path,
    sessions: project.sessions,
    pendingInteractions: project.path === selectedProjectPath ? pendingInteractions : project.pendingInteractions,
  }));
  const visibleProjectCount = sessionProjectGroups.length;
  const workspaceStyle = {
    "--sidebar-width": `${layout.sidebarWidth}px`,
    "--context-width": `${layout.contextWidth}px`,
  } as CSSProperties;
  return (
    <div className="shell">
      <div className="ambient ambient-left" />
      <div className="ambient ambient-right" />
      <main
        ref={workspaceRef}
        className={`workspace ${contextPanelOpen ? "context-open" : "context-collapsed"} ${layoutDragging ? "resizing" : ""}`}
        style={workspaceStyle}
      >
        <aside className="panel sidebar-panel">
          <div className="panel-header">
            <div>
              <h2>Projects</h2>
            </div>
            <div className="panel-header-actions">
              <span className="panel-count">{visibleProjectCount} total</span>
              <button
                className="action primary sidebar-new"
                onClick={() => void handleCreateProject()}
                disabled={busyAction !== null}
                title="New Project"
                aria-label="New Project"
              >
                +
              </button>
            </div>
          </div>

          <div className="session-list">
            {sessionProjectGroups.length === 0 ? (
              <div className="empty-card">
                <p>No projects yet.</p>
                <span>Choose a folder to add a project and run its own Somnia sidecar.</span>
              </div>
            ) : (
              <div className="project-groups">
                {sessionProjectGroups.map((group) => {
                  const isCollapsed = Boolean(collapsedProjects[group.key]);
                  return (
                    <section key={group.key} className="project-group">
                      <div className={`project-toggle ${isCollapsed ? "collapsed" : ""}`}>
                        <button
                          className="project-toggle-button"
                          onClick={() =>
                            setCollapsedProjects((previous) => ({
                              ...previous,
                              [group.key]: !previous[group.key],
                            }))
                          }
                        >
                          <span className="project-toggle-main">
                            <span className="project-toggle-caret">{isCollapsed ? "▸" : "▾"}</span>
                            <span className="project-toggle-label">
                              <strong>{group.label}</strong>
                              <small>{group.path}</small>
                            </span>
                          </span>
                          <span className="project-toggle-count">{group.sessions.length}</span>
                        </button>
                        <div className="project-menu" ref={projectMenuOpenKey === group.key ? projectMenuRef : null}>
                          <button
                            className="project-menu-trigger"
                            onClick={(event) => {
                              event.stopPropagation();
                              setProjectMenuOpenKey((current) => (current === group.key ? null : group.key));
                            }}
                            aria-label={`Project options for ${group.label}`}
                            title="Project options"
                          >
                            ⋯
                          </button>
                          {projectMenuOpenKey === group.key ? (
                            <div className="project-menu-panel">
                              <button
                                className="project-menu-item"
                                onClick={() => {
                                  setProjectMenuOpenKey(null);
                                  void handleCreateSession(group.path);
                                }}
                                disabled={busyAction !== null}
                              >
                                New
                              </button>
                              <button
                                className="project-menu-item danger"
                                onClick={() => {
                                  void handleRemoveProject(group.path);
                                }}
                                disabled={busyAction !== null}
                              >
                                Remove
                              </button>
                            </div>
                          ) : null}
                        </div>
                      </div>
                      {isCollapsed ? null : (
                        <div className="project-session-list">
                          {group.sessions.map((session) => {
                            const isSelected = selectedProjectPath === group.path && selectedSessionId === session.id;
                            const isAnswering = (activeProjectTurns[group.path] ?? []).some((turn) => turn.sessionId === session.id);
                            const isWaitingForDecision = group.pendingInteractions.some((interaction) => interaction.session_id === session.id);
                            return (
                              <button
                                key={session.id}
                                className={`session-card ${isSelected ? "selected" : ""} ${isAnswering ? "answering" : ""} ${isWaitingForDecision ? "waiting-decision" : ""}`}
                                onClick={() => {
                                  setContextPanelOpen(true);
                                  void activateProject(group.path, projectClientsRef.current[group.path]).then(() =>
                                    selectSession(session.id, projectClientsRef.current[group.path], group.sessions, group.path),
                                  );
                                }}
                              >
                                <div className="session-card-head">
                                  <strong>{session.id}</strong>
                                  <span className="session-card-status">
                                    <span>{formatRelativeTime(session.updated_at ?? session.created_at)}</span>
                                    {isWaitingForDecision ? (
                                      <span className="session-decision-indicator" aria-label="Waiting for your decision" />
                                    ) : isAnswering ? (
                                      <span className="session-answering-indicator" aria-label="Agent is responding">
                                        <span aria-hidden="true" />
                                        <span aria-hidden="true" />
                                        <span aria-hidden="true" />
                                      </span>
                                    ) : null}
                                  </span>
                                </div>
                                <p>{buildSessionPreview(session)}</p>
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </section>
                  );
                })}
              </div>
            )}
          </div>
        </aside>

        <div
          className="layout-resizer sidebar-resizer"
          role="separator"
          aria-label="Resize projects panel"
          aria-orientation="vertical"
          onPointerDown={(event) => beginLayoutDrag("sidebar", event)}
        />

        <section className="panel conversation-panel">
          <div className="panel-header conversation-header">
            <div className="conversation-heading">
              <h2 title={conversationPreview || selectedSessionId || "New conversation"}>{conversationTitle}</h2>
              <button
                className="workspace-link"
                onClick={() => {
                  if (workspaceRootPath) {
                    void openWorkspaceRoot(workspaceRootPath);
                  }
                }}
                disabled={!workspaceRootPath}
                title={workspaceRootPath || "Workspace unavailable"}
              >
                {workspaceRootName}
              </button>
            </div>
            <div className="status-cluster">
              <button
                className="action ghost detail-toggle"
                onClick={() => setContextPanelOpen((current) => !current)}
                title={contextPanelOpen ? "Hide details" : "Show details"}
                aria-label={contextPanelOpen ? "Hide details" : "Show details"}
              >
                ⋯
              </button>
            </div>
          </div>

          <TodoStatusBar summary={todoSummary} expanded={todoExpanded} onToggleExpanded={() => setTodoExpanded((current) => !current)} />

          <div className="conversation-body">
            {conversationRows.length === 0 && activeQueuedPrompts.length === 0 && !currentSessionInteraction ? (
              <div className="empty-conversation">
                <h3>Start a session</h3>
                <p>Connect to a sidecar, choose a session, then send a prompt. Streaming output lands here.</p>
              </div>
            ) : (
              conversationRows.map((row) => (
                <article key={row.id} className={`bubble ${row.role} ${row.isPending ? "pending" : ""}`}>
                  {row.isStreaming ? <div className="bubble-status">Streaming</div> : null}
                  {row.text ? <MarkdownMessage text={row.text} /> : null}
                  {row.isLoading ? (
                    <span className="typing-indicator" aria-label="Waiting for assistant response">
                      <span />
                      <span />
                      <span />
                    </span>
                  ) : null}
                  {row.toolCalls?.length ? (
                    <div className="tool-call-stack">
                      {row.toolCalls.map((toolCall) => (
                        <details key={toolCall.id} className="tool-call-card">
                          <summary>
                            <span>{toolCall.name}</span>
                            {toolCall.logId ? <em>{toolCall.logId}</em> : null}
                          </summary>
                          <div className="tool-call-detail">
                            <span>Input</span>
                            <pre>{toolCall.input}</pre>
                          </div>
                          <div className="tool-call-detail">
                            <span>Output</span>
                            <pre>{toolCall.output}</pre>
                          </div>
                        </details>
                      ))}
                    </div>
                  ) : null}
                </article>
              ))
            )}
            {activeQueuedPrompts.length > 0 ? (
              <PromptQueueCard
                prompts={activeQueuedPrompts}
                canInject={currentSessionRunning}
                busy={busyAction !== null}
                onInject={handleQueuePromptInjection}
              />
            ) : null}
            {currentSessionInteraction ? (
              <InteractionDecisionCard
                interaction={currentSessionInteraction}
                busy={busyAction !== null}
                onResolveAuthorization={handleResolveAuthorization}
                onResolveModeSwitch={handleResolveModeSwitch}
              />
            ) : null}
          </div>

          <div className="composer">
            <textarea
              ref={composerTextareaRef}
              value={draft}
              onChange={(event) => handleComposerChange(event.target.value)}
              onKeyDown={handleComposerKeyDown}
              onPaste={(event) => void handleComposerPaste(event)}
              placeholder="Ask Somnia to inspect, plan, or implement against the current workspace."
              rows={1}
            />
            {pendingImages.length > 0 ? (
              <div className="pending-attachments">
                {pendingImages.map((image) => (
                  <button
                    key={image.id}
                    className="pending-attachment"
                    onClick={() => removePendingImage(image.id)}
                    title={`Remove ${image.name}`}
                  >
                    <span>{image.name}</span>
                    <strong>x</strong>
                  </button>
                ))}
              </div>
            ) : null}
            {commandPickerOpen && commandSuggestions.length > 0 ? (
              <div className="command-picker">
                {commandSuggestions.map((item, index) => (
                  <button
                    key={item.command}
                    className={`command-option ${index === selectedCommandIndex ? "selected" : ""}`}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      applyCommandSuggestion(item.command);
                    }}
                  >
                    <strong>{item.command}</strong>
                    <span>{item.description}</span>
                  </button>
                ))}
              </div>
            ) : null}
            <div className="composer-actions">
              <input
                ref={fileInputRef}
                className="file-input"
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                multiple
                onChange={(event) => void handleImageFilesSelected(event.currentTarget.files)}
              />
              <button
                className="action secondary composer-icon-action attachment-action"
                onClick={() => fileInputRef.current?.click()}
                disabled={busyAction !== null}
                title="Attach image"
                aria-label="Attach image"
              >
                +
              </button>
              <div className="composer-meta">
                <div className="composer-controls">
                  <div className="model-picker" ref={modelPickerRef}>
                    <button
                      className={`model-trigger ${modelPickerOpen ? "open" : ""}`}
                      onClick={() => setModelPickerOpen((current) => !current)}
                      disabled={providers.length === 0 || busyAction !== null}
                    >
                      <span>{`${activeProviderLabel} / ${activeModelLabel}`}</span>
                      <span className="model-trigger-meta">
                        <span className="model-trigger-caret">{activeReasoningLabel}</span>
                        <span
                          className={`connection-dot ${connectionState === "connected" ? "connected" : "attention"}`}
                          aria-label={connectionState}
                          title={connectionState}
                        />
                      </span>
                    </button>
                    {modelPickerOpen ? (
                      <div className="model-picker-panel">
                        <div className="model-picker-grid">
                          <div className="picker-column">
                            <span className="picker-label">Provider</span>
                            <div className="picker-options">
                              {providers.map((provider) => (
                                <button
                                  key={provider.name}
                                  className={`picker-option ${selectedProvider === provider.name ? "selected" : ""}`}
                                  onClick={() => void handleProviderChange(provider.name)}
                                  disabled={busyAction !== null}
                                >
                                  {provider.name}
                                </button>
                              ))}
                            </div>
                          </div>
                          <div className="picker-column">
                            <span className="picker-label">Model</span>
                            <div className="picker-options">
                              {models.map((model) => (
                                <button
                                  key={model.name}
                                  className={`picker-option ${selectedModel === model.name ? "selected" : ""}`}
                                  onClick={() => setSelectedModel(model.name)}
                                  disabled={busyAction !== null}
                                >
                                  {model.name}
                                </button>
                              ))}
                            </div>
                          </div>
                        </div>
                        <div className="model-picker-footer">
                          <div className="reasoning-levels" role="group" aria-label="Reasoning level">
                            {REASONING_LEVEL_OPTIONS.map((level) => (
                              <button
                                key={level}
                                className={`reasoning-option ${selectedReasoningLevel === level ? "selected" : ""}`}
                                onClick={() => setSelectedReasoningLevel(level)}
                                disabled={busyAction !== null}
                              >
                                {formatReasoningLevel(level)}
                              </button>
                            ))}
                          </div>
                          <button
                            className="action secondary picker-apply"
                            onClick={() => void handleApplyProviderModel()}
                            disabled={!selectedProvider || !selectedModel || busyAction !== null}
                          >
                            Apply
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </div>
                  <div className="mode-picker" ref={modePickerRef}>
                    <button
                      className={`mode-pill ${modePickerOpen ? "open" : ""}`}
                      onClick={() => setModePickerOpen((current) => !current)}
                      disabled={!clientRef.current || busyAction !== null}
                    >
                      {activeExecutionModeLabel}
                    </button>
                    {modePickerOpen ? (
                      <div className="mode-picker-panel">
                        {EXECUTION_MODE_OPTIONS.map((mode) => (
                          <button
                            key={mode.key}
                            className={`mode-option ${activeExecutionMode === mode.key ? "selected" : ""}`}
                            onClick={() => void handleExecutionModeChange(mode.key)}
                            disabled={busyAction !== null}
                          >
                            <strong>{mode.title}</strong>
                            <span>{mode.description}</span>
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  <div
                    className="ctx-meter"
                    style={
                      {
                        "--ctx-color": contextColor,
                        "--ctx-fill": `${contextFill}%`,
                      } as CSSProperties
                    }
                    title={contextTitle}
                    aria-label={contextTitle}
                  >
                    <span className="ctx-ring" />
                    <span className="ctx-label">{contextLabel}</span>
                  </div>
                </div>
              </div>
              <div className="composer-cta">
              <button
                className="action primary composer-icon-action"
                onClick={() => void handleSendPrompt()}
                  disabled={
                    (!draft.trim() && pendingImages.length === 0) ||
                    busyAction !== null ||
                    (projectTurnLimitReached && !currentSessionRunning)
                  }
                  title={
                    currentSessionRunning
                      ? "Queue for this session"
                      : projectTurnLimitReached
                        ? "This project already has two sessions running"
                        : "Send"
                  }
                  aria-label="Send"
                >
                  ↑
                </button>
                {currentSessionTurn ? (
                  <button
                    className="action danger composer-icon-action"
                    onClick={() => void handleInterrupt()}
                    disabled={busyAction !== null}
                    title="Interrupt"
                    aria-label="Interrupt"
                  >
                    ■
                  </button>
                ) : null}
              </div>
            </div>
          </div>
        </section>

        {contextPanelOpen ? (
          <>
          <div
            className="layout-resizer context-resizer"
            role="separator"
            aria-label="Resize session details panel"
            aria-orientation="vertical"
            onPointerDown={(event) => beginLayoutDrag("context", event)}
          />
          <aside className="panel context-panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Context</p>
                <h2>Session Details</h2>
              </div>
              <button className="action ghost" onClick={() => setContextPanelOpen(false)}>
                Collapse
              </button>
            </div>
            <div className="inspector-section context-scroll">
              {currentSession ? (
                <>
                  <div className="fact-row">
                    <span>Session</span>
                    <strong>{currentSession.id}</strong>
                  </div>
                  <div className="fact-row">
                    <span>Updated</span>
                    <strong>{formatRelativeTime(currentSession.updated_at ?? currentSession.created_at)}</strong>
                  </div>
                  <div className="fact-row">
                    <span>Messages</span>
                    <strong>{currentSession.messages.length}</strong>
                  </div>
                  <div className="fact-row">
                    <span>Current mode</span>
                    <strong>{status?.execution_mode_title ?? "unknown"}</strong>
                  </div>
                  <div className="context-block">
                    <h3>Preview</h3>
                    <p>{buildSessionPreview(currentSession)}</p>
                  </div>
                </>
              ) : (
                <div className="empty-card">
                  <p>No session selected.</p>
                  <span>Choose a session from the sidebar to inspect its details.</span>
                </div>
              )}
            </div>
          </aside>
          </>
        ) : null}
      </main>

    </div>
  );
}

function InteractionDecisionCard({
  interaction,
  busy,
  onResolveAuthorization,
  onResolveModeSwitch,
}: {
  interaction: InteractionRequestState;
  busy: boolean;
  onResolveAuthorization: (
    interactionId: string,
    scope: "once" | "workspace" | "deny",
    approved: boolean,
    reason: string,
  ) => Promise<void>;
  onResolveModeSwitch: (interaction: InteractionRequestState, approved: boolean) => Promise<void>;
}) {
  const isAuthorization = interaction.kind === "authorization";
  return (
    <section className="decision-card" aria-live="polite">
      <div className="decision-copy">
        <p className="eyebrow">{isAuthorization ? "Authorization request" : "Mode switch request"}</p>
        <h3>{interactionTitle(interaction)}</h3>
        <p>{interactionSummary(interaction)}</p>
      </div>
      {isAuthorization ? (
        <div className="decision-actions">
          <button
            className="action primary"
            onClick={() => void onResolveAuthorization(interaction.id, "once", true, "Allowed once from desktop UI.")}
            disabled={busy}
          >
            Allow once
          </button>
          <button
            className="action secondary"
            onClick={() => void onResolveAuthorization(interaction.id, "workspace", true, "Allowed in this workspace from desktop UI.")}
            disabled={busy}
          >
            Allow workspace
          </button>
          <button
            className="action danger"
            onClick={() => void onResolveAuthorization(interaction.id, "deny", false, "Denied from desktop UI.")}
            disabled={busy}
          >
            Deny
          </button>
        </div>
      ) : (
        <div className="decision-actions">
          <button className="action primary" onClick={() => void onResolveModeSwitch(interaction, true)} disabled={busy}>
            Switch now
          </button>
          <button className="action danger" onClick={() => void onResolveModeSwitch(interaction, false)} disabled={busy}>
            Stay here
          </button>
        </div>
      )}
    </section>
  );
}

function PromptQueueCard({
  prompts,
  canInject,
  busy,
  onInject,
}: {
  prompts: QueuedPrompt[];
  canInject: boolean;
  busy: boolean;
  onInject: (prompt: QueuedPrompt) => Promise<void>;
}) {
  return (
    <section className="prompt-queue-card" aria-live="polite">
      <div className="prompt-queue-head">
        <p className="eyebrow">Queued prompts</p>
        <span>{prompts.length}</span>
      </div>
      <ol>
        {prompts.map((prompt) => (
          <li key={prompt.id}>
            <span>{prompt.userText}</span>
            <button
              className="queue-inject-button"
              onClick={() => void onInject(prompt)}
              disabled={!canInject || busy || prompt.injectionRequested}
              title={prompt.injectionRequested ? "Waiting for the next agent loop" : "Inject on next agent loop"}
            >
              {prompt.injectionRequested ? "Next loop" : "Inject next loop"}
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

function MarkdownMessage({ text }: { text: string }) {
  return <div className="markdown-content">{renderMarkdownBlocks(text)}</div>;
}

function renderMarkdownBlocks(text: string): ReactNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  let key = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fenceMatch = line.match(/^\s*```([^`]*)\s*$/);
    if (fenceMatch) {
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        index += 1;
      }
      const language = fenceMatch[1].trim();
      blocks.push(
        <pre key={`block-${key++}`} className="markdown-code-block">
          {language ? <span className="markdown-code-language">{language}</span> : null}
          <code>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    if (/^\s*---+\s*$/.test(line)) {
      blocks.push(<hr key={`block-${key++}`} />);
      index += 1;
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = Math.min(headingMatch[1].length, 6);
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={`block-${key++}`}>{renderInlineMarkdown(headingMatch[2])}</Tag>);
      index += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoteLines: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(<blockquote key={`block-${key++}`}>{renderInlineMarkdown(quoteLines.join("\n"))}</blockquote>);
      continue;
    }

    const unorderedMatch = line.match(/^(\s*)[-*+]\s+(.+)$/);
    if (unorderedMatch) {
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*[-*+]\s+(.+)$/);
        if (!itemMatch) {
          break;
        }
        items.push(<li key={`item-${items.length}`}>{renderInlineMarkdown(itemMatch[1])}</li>);
        index += 1;
      }
      blocks.push(<ul key={`block-${key++}`}>{items}</ul>);
      continue;
    }

    const orderedMatch = line.match(/^(\s*)\d+[.)]\s+(.+)$/);
    if (orderedMatch) {
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!itemMatch) {
          break;
        }
        items.push(<li key={`item-${items.length}`}>{renderInlineMarkdown(itemMatch[1])}</li>);
        index += 1;
      }
      blocks.push(<ol key={`block-${key++}`}>{items}</ol>);
      continue;
    }

    const paragraphLines = [line.trim()];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^\s*```/.test(lines[index]) &&
      !/^\s*---+\s*$/.test(lines[index]) &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      !/^\s*>\s?/.test(lines[index]) &&
      !/^\s*[-*+]\s+/.test(lines[index]) &&
      !/^\s*\d+[.)]\s+/.test(lines[index])
    ) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    blocks.push(<p key={`block-${key++}`}>{renderInlineMarkdown(paragraphLines.join(" "))}</p>);
  }

  return blocks;
}

function renderInlineMarkdown(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  let key = 0;

  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > cursor) {
      nodes.push(text.slice(cursor, start));
    }
    const token = match[0];
    if (token.startsWith("`")) {
      nodes.push(<code key={`inline-${key++}`}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**") || token.startsWith("__")) {
      nodes.push(<strong key={`inline-${key++}`}>{renderInlineMarkdown(token.slice(2, -2))}</strong>);
    } else if (token.startsWith("*") || token.startsWith("_")) {
      nodes.push(<em key={`inline-${key++}`}>{renderInlineMarkdown(token.slice(1, -1))}</em>);
    } else {
      const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (linkMatch) {
        nodes.push(
          <a key={`inline-${key++}`} href={linkMatch[2]} target="_blank" rel="noreferrer">
            {renderInlineMarkdown(linkMatch[1])}
          </a>,
        );
      } else {
        nodes.push(token);
      }
    }
    cursor = start + token.length;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function upsertProject(projects: ProjectState[], nextProject: ProjectState): ProjectState[] {
  const others = projects.filter((project) => project.path !== nextProject.path);
  return [...others, nextProject].sort((left, right) => left.label.localeCompare(right.label));
}

function readStoredLayout(): LayoutState {
  const fallback = { sidebarWidth: 250, contextWidth: 340 };
  if (typeof window === "undefined") {
    return fallback;
  }
  try {
    const value = JSON.parse(window.localStorage.getItem(LAYOUT_STORAGE_KEY) ?? "{}") as Partial<LayoutState>;
    return {
      sidebarWidth: clampNumber(Number(value.sidebarWidth) || fallback.sidebarWidth, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH),
      contextWidth: clampNumber(Number(value.contextWidth) || fallback.contextWidth, CONTEXT_MIN_WIDTH, CONTEXT_MAX_WIDTH),
    };
  } catch {
    return fallback;
  }
}

function persistLayout(layout: LayoutState) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layout));
}

function nextDraggedLayout(
  drag: LayoutDragState,
  clientX: number,
  containerWidth: number,
  contextPanelOpen: boolean,
): LayoutState {
  const delta = clientX - drag.startX;
  const resizerSpace = contextPanelOpen ? RESIZER_WIDTH * 2 : RESIZER_WIDTH;
  const maxSidebarByContainer = containerWidth - CONVERSATION_MIN_WIDTH - resizerSpace - (contextPanelOpen ? drag.startContextWidth : 0);
  const maxContextByContainer = containerWidth - drag.startSidebarWidth - CONVERSATION_MIN_WIDTH - resizerSpace;

  if (drag.target === "sidebar") {
    return {
      sidebarWidth: clampNumber(drag.startSidebarWidth + delta, SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, maxSidebarByContainer)),
      contextWidth: drag.startContextWidth,
    };
  }

  return {
    sidebarWidth: drag.startSidebarWidth,
    contextWidth: clampNumber(drag.startContextWidth - delta, CONTEXT_MIN_WIDTH, Math.min(CONTEXT_MAX_WIDTH, maxContextByContainer)),
  };
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.round(Math.max(min, Math.min(value, Math.max(min, max))));
}

function TodoStatusBar({
  summary,
  expanded,
  onToggleExpanded,
}: {
  summary: TodoSummary | null;
  expanded: boolean;
  onToggleExpanded: () => void;
}) {
  if (!summary || summary.openItems.length === 0) {
    return null;
  }

  const focusItem = summary.activeItem ?? summary.nextItem ?? summary.openItems[0] ?? null;
  const focusStatus = normalizeTodoStatus(focusItem?.status);
  const focusPrefix = focusStatus === "in_progress" ? "In progress" : "Next";
  const focusLabel = focusItem ? formatTodoLabel(focusItem) : "";
  const shownItems = expanded ? summary.visibleItems.slice(0, 5) : [];
  const hiddenCount = Math.max(0, summary.visibleItems.length - shownItems.length);

  return (
    <section className={`todo-status-bar ${expanded ? "expanded" : ""}`} aria-label="Todo progress">
      <div className="todo-status-main">
        <div className="todo-status-pulse" aria-hidden="true" />
        <div className="todo-status-copy">
          <div className="todo-status-line">
            <strong>Todo</strong>
            <span>
              {summary.completedCount}/{summary.visibleItems.length} done
            </span>
          </div>
          {focusLabel ? (
            <p>
              <span>{focusPrefix}:</span> {focusLabel}
            </p>
          ) : null}
        </div>
        <button className="todo-toggle" type="button" onClick={onToggleExpanded}>
          {expanded ? "Hide" : "Show all"}
        </button>
      </div>
      {expanded ? (
        <div className="todo-status-list">
          {shownItems.map((item, index) => {
            const status = normalizeTodoStatus(item.status);
            const label = formatTodoLabel(item);
            return (
              <div key={`${status}-${index}-${label}`} className={`todo-status-item ${status}`}>
                <span aria-hidden="true">{todoStatusMarker(status)}</span>
                <p>{label || "(untitled todo)"}</p>
              </div>
            );
          })}
          {hiddenCount > 0 ? <div className="todo-status-more">+{hiddenCount} more</div> : null}
        </div>
      ) : null}
    </section>
  );
}

function buildTodoSummary(items: TodoItem[] | undefined): TodoSummary | null {
  const visibleItems = (items ?? []).filter((item) => TODO_VISIBLE_STATUSES.has(normalizeTodoStatus(item.status)));
  const openItems = visibleItems.filter((item) => TODO_OPEN_STATUSES.has(normalizeTodoStatus(item.status)));
  if (openItems.length === 0) {
    return null;
  }
  return {
    visibleItems,
    openItems,
    completedCount: visibleItems.filter((item) => normalizeTodoStatus(item.status) === "completed").length,
    activeItem: visibleItems.find((item) => normalizeTodoStatus(item.status) === "in_progress") ?? null,
    nextItem: visibleItems.find((item) => normalizeTodoStatus(item.status) === "pending") ?? null,
  };
}

const TODO_OPEN_STATUSES = new Set(["pending", "in_progress"]);
const TODO_VISIBLE_STATUSES = new Set(["pending", "in_progress", "completed"]);

function normalizeTodoStatus(status: unknown): string {
  const normalized = String(status ?? "pending")
    .trim()
    .toLowerCase();
  return normalized || "pending";
}

function todoStatusMarker(status: string): string {
  if (status === "in_progress") {
    return "⏳";
  }
  if (status === "completed") {
    return "✅";
  }
  return "☐";
}

function readStoredProjectPaths(): string[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const value = JSON.parse(window.localStorage.getItem(PROJECTS_STORAGE_KEY) ?? "[]") as unknown;
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
  } catch {
    return [];
  }
}

function persistProjectPath(projectPath: string) {
  if (typeof window === "undefined" || !projectPath.trim()) {
    return;
  }
  const paths = readStoredProjectPaths();
  if (!paths.includes(projectPath)) {
    window.localStorage.setItem(PROJECTS_STORAGE_KEY, JSON.stringify([...paths, projectPath]));
  }
}

function removeStoredProjectPath(projectPath: string) {
  if (typeof window === "undefined") {
    return;
  }
  const paths = readStoredProjectPaths().filter((path) => path !== projectPath);
  window.localStorage.setItem(PROJECTS_STORAGE_KEY, JSON.stringify(paths));
}

function readStoredPromptHistory(): string[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const value = JSON.parse(window.localStorage.getItem(PROMPT_HISTORY_STORAGE_KEY) ?? "[]") as unknown;
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
  } catch {
    return [];
  }
}

function persistPromptHistory(history: string[]) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(PROMPT_HISTORY_STORAGE_KEY, JSON.stringify(history));
}

function currentCommandSuggestions(value: string): Array<(typeof COMMAND_SPECS)[number]> {
  const query = value.trimStart();
  if (!/^\/[^\s]*$/.test(query)) {
    return [];
  }
  return COMMAND_SPECS.filter((item) => item.command.startsWith(query)).slice(0, 8);
}

function buildPromptPayload(prompt: string, images: PendingImage[]): string | Record<string, unknown> {
  if (images.length === 0) {
    return prompt;
  }
  const content: Array<Record<string, unknown>> = [];
  const text = prompt.trim() || "Look at this image.";
  if (text) {
    content.push({ type: "text", text });
  }
  for (const image of images) {
    content.push({
      type: "image_url",
      image_url: {
        url: image.dataUrl,
      },
      media_type: image.mediaType,
      name: image.name,
    });
  }
  return { role: "user", content };
}

function buildOptimisticUserText(prompt: string, images: PendingImage[]): string {
  const text = prompt.trim() || (images.length > 0 ? "Look at this image." : "");
  if (images.length === 0) {
    return text;
  }
  const attachmentLabel = images.length === 1 ? "[1 image attached]" : `[${images.length} images attached]`;
  return text ? `${text}\n\n${attachmentLabel}` : attachmentLabel;
}

function readClipboardImage(file: File): Promise<PendingImage> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("Failed to read image."));
    reader.onload = () => {
      const dataUrl = typeof reader.result === "string" ? reader.result : "";
      if (!dataUrl.startsWith("data:image/")) {
        reject(new Error("Clipboard item is not a supported image."));
        return;
      }
      resolve({
        id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        name: file.name || "pasted-image",
        mediaType: file.type || dataUrl.slice(5, dataUrl.indexOf(";")) || "image/png",
        dataUrl,
      });
    };
    reader.readAsDataURL(file);
  });
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

function findSessionInteraction(interactions: InteractionRequestState[], sessionId: string): InteractionRequestState | null {
  return interactions.find((interaction) => interaction.session_id === sessionId) ?? null;
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

function normalizeReasoningLevel(value: string | null | undefined): ReasoningLevelOption {
  const normalized = String(value ?? "")
    .trim()
    .toLowerCase();
  return (REASONING_LEVEL_OPTIONS as readonly string[]).includes(normalized) ? (normalized as ReasoningLevelOption) : "auto";
}

function normalizeExecutionMode(value: string | null | undefined): ExecutionModeOption {
  const normalized = String(value ?? "")
    .trim()
    .toLowerCase();
  return (EXECUTION_MODE_OPTIONS as readonly { key: string }[]).some((mode) => mode.key === normalized)
    ? (normalized as ExecutionModeOption)
    : "accept_edits";
}

function formatReasoningLevel(value: string | null | undefined): string {
  const normalized = normalizeReasoningLevel(value);
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

function normalizeContextPercent(value: number | null | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  return Math.max(0, Math.min(100, value));
}

function contextUsageColor(percent: number | null): string {
  if (percent === null) {
    return "#7dd3fc";
  }
  if (percent <= 30) {
    return "#22c55e";
  }
  if (percent <= 60) {
    return "#84cc16";
  }
  if (percent <= 80) {
    return "#f59e0b";
  }
  return "#ef4444";
}

function formatTokenCount(tokenCount: number | null | undefined): string {
  const value = Math.max(0, Number(tokenCount) || 0);
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(2)}M`;
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(1)}k`;
  }
  return String(Math.round(value));
}

function truncateTopic(value: string, maxLength = 15): string {
  const normalized = String(value || "").trim();
  if (!normalized) {
    return "New conversation";
  }
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength)}...`;
}

function getPathLeafName(path: string): string {
  const normalized = String(path || "").replace(/[\\/]+$/, "");
  if (!normalized) {
    return "workspace";
  }
  const segments = normalized.split(/[\\/]/).filter(Boolean);
  return segments[segments.length - 1] || normalized;
}

export default App;
