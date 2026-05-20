import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ClipboardEvent as ReactClipboardEvent,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import mermaid from "mermaid";
import appIconUrl from "../src-tauri/icons/32x32.png";

import {
  chooseProjectFolder,
  closeMainWindow,
  ensureManagedSidecar,
  isMainWindowMaximized,
  minimizeMainWindow,
  onMainWindowResized,
  openWorkspaceRoot,
  startMainWindowDrag,
  stopManagedSidecar,
  toggleMaximizeMainWindow,
} from "./lib/desktop";
import {
  buildConversationRows,
  buildSessionPreview,
  formatRelativeTime,
  formatTodoLabel,
  sortSessions,
  stringifyToolValue,
} from "./lib/messages";
import SettingsView, { type ArchivedSessionEntry, type SettingsSectionKey } from "./components/SettingsView";
import { useI18n, type TranslationKey } from "./lib/i18n";
import { SidecarClient, normalizeBaseUrl } from "./lib/sidecar";
import type {
  AgentSession,
  ConversationContentBlock,
  ConversationPendingTurn,
  ConversationRuntimeItem,
  ConversationToolCall,
  InteractionRequestState,
  ManagedSidecarConnection,
  McpServerSummary,
  ModelDescriptor,
  ProviderDescriptor,
  SettingsConfigScope,
  SettingsConfigScopeKey,
  SettingsConfigSectionKey,
  SidecarEvent,
  SidecarStatus,
  TaskGraphItem,
  TeamMemberActivity,
  TodoItem,
  ToolLogDetail,
  ToolLogIndexEntry,
  WorkspacePathSuggestion,
} from "./types";

const STORAGE_KEY = "somnia.desktop.sidecar-url";
const PROJECTS_STORAGE_KEY = "somnia.desktop.project-paths";
const PROMPT_HISTORY_STORAGE_KEY = "somnia.desktop.prompt-history";
const LAYOUT_STORAGE_KEY = "somnia.desktop.layout";
const ARCHIVED_SESSIONS_STORAGE_KEY = "somnia.desktop.archived-sessions";
const DEFAULT_SIDECAR_URL = "http://127.0.0.1:8765";
const TOOL_LIMIT = 24;
const SIDEBAR_MIN_WIDTH = 210;
const SIDEBAR_MAX_WIDTH = 430;
const CONTEXT_MIN_WIDTH = 280;
const CONTEXT_MAX_WIDTH = 540;
const CONVERSATION_MIN_WIDTH = 430;
const RESIZER_WIDTH = 10;
const REASONING_LEVEL_OPTIONS = ["auto", "low", "medium", "high", "deep"] as const;
let mermaidRenderCounter = 0;
const MERMAID_MIN_ZOOM = 0.25;
const MERMAID_MAX_ZOOM = 4;
const MERMAID_ZOOM_STEP = 0.2;

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "dark",
  fontFamily: '"Segoe UI Variable", "Aptos", "IBM Plex Sans", sans-serif',
});

const COMMAND_SPECS = [
  { command: "/scan", descriptionKey: "cmd.scan" as const },
  { command: "/symbols", descriptionKey: "cmd.symbols" as const },
  { command: "/image", descriptionKey: "cmd.image" as const },
  { command: "/paste-image", descriptionKey: "cmd.pasteImage" as const },
  { command: "/model", descriptionKey: "cmd.model" as const },
  { command: "/reasoning", descriptionKey: "cmd.reasoning" as const },
  { command: "/providers", descriptionKey: "cmd.providers" as const },
  { command: "/hooks", descriptionKey: "cmd.hooks" as const },
  { command: "/undo", descriptionKey: "cmd.undo" as const },
  { command: "/checkpoint", descriptionKey: "cmd.checkpoint" as const },
  { command: "/rollback", descriptionKey: "cmd.rollback" as const },
  { command: "/compact", descriptionKey: "cmd.compact" as const },
  { command: "/janitor", descriptionKey: "cmd.janitor" as const },
  { command: "/skills", descriptionKey: "cmd.skills" as const },
  { command: "/tasks", descriptionKey: "cmd.tasks" as const },
  { command: "/team", descriptionKey: "cmd.team" as const },
  { command: "/mcp", descriptionKey: "cmd.mcp" as const },
  { command: "/bg", descriptionKey: "cmd.bg" as const },
  { command: "/help", descriptionKey: "cmd.help" as const },
  { command: "/exit", descriptionKey: "cmd.exit" as const },
] as const;
const PATH_MENTION_PATTERN = /(^|\s)@([^\s]*)$/;
const EXECUTION_MODE_OPTIONS = [
  { key: "shortcuts", titleKey: "mode.shortcuts.title" as const, descriptionKey: "mode.shortcuts.description" as const },
  { key: "plan", titleKey: "mode.plan.title" as const, descriptionKey: "mode.plan.description" as const },
  { key: "accept_edits", titleKey: "mode.acceptEdits.title" as const, descriptionKey: "mode.acceptEdits.description" as const },
  { key: "yolo", titleKey: "mode.yolo.title" as const, descriptionKey: "mode.yolo.description" as const },
] as const;
type ReasoningLevelOption = (typeof REASONING_LEVEL_OPTIONS)[number];
type ExecutionModeOption = (typeof EXECUTION_MODE_OPTIONS)[number]["key"];
type PendingImage = {
  id: string;
  name: string;
  mediaType: string;
  dataUrl: string;
};
type PreparedPromptPayload = string | Record<string, unknown>;
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
  connection: ManagedSidecarConnection | null;
  status: SidecarStatus | null;
  connectionState: "connecting" | "connected" | "error";
  connectionError?: string | null;
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
type ArchivedSessionsState = Record<string, string[]>;
type ActiveProjectTurn = {
  sessionId: string;
  turnId: string | null;
};
type SubagentActivity = {
  id: string;
  prompt: string;
  agentType: string;
  startedAt: number;
  lastActivityAt?: number;
  facts: string[];
};
type TaskGraphNodeLayout = {
  task: TaskGraphItem;
  x: number;
  y: number;
  level: number;
};
type TaskGraphEdge = {
  from: number;
  to: number;
};
type TaskGraphLayout = {
  nodes: TaskGraphNodeLayout[];
  edges: TaskGraphEdge[];
  width: number;
  height: number;
  nodeWidth: number;
  nodeHeight: number;
};
type ConfigCommandTarget = SettingsConfigSectionKey | "skills";
type ContextCommandTarget = "compact" | "janitor";
type UiCommandTarget =
  | { kind: "config"; target: ConfigCommandTarget }
  | { kind: "model" }
  | { kind: "context"; command: ContextCommandTarget };

const DEFAULT_CONVERSATION_PROJECT_KEY = "__default_project__";
const SUBAGENT_FACTS_LIMIT = 5;

function App() {
  const { t } = useI18n();
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
  const [runtimeConversationItems, setRuntimeConversationItems] = useState<Record<string, ConversationRuntimeItem[]>>({});
  const [pendingTurns, setPendingTurns] = useState<Record<string, ConversationPendingTurn>>({});
  const [queuedPrompts, setQueuedPrompts] = useState<Record<string, QueuedPrompt[]>>({});
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [activeProjectTurns, setActiveProjectTurns] = useState<Record<string, ActiveProjectTurn[]>>({});
  const [activeSubagents, setActiveSubagents] = useState<Record<string, Record<string, SubagentActivity>>>({});
  const [teamActivity, setTeamActivity] = useState<Record<string, TeamMemberActivity[]>>({});
  const [taskGraph, setTaskGraph] = useState<Record<string, TaskGraphItem[]>>({});
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
  const [pathPickerOpen, setPathPickerOpen] = useState(false);
  const [pathSuggestions, setPathSuggestions] = useState<WorkspacePathSuggestion[]>([]);
  const [selectedPathIndex, setSelectedPathIndex] = useState(0);
  const [composerCursor, setComposerCursor] = useState(0);
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);
  const [toolLogs, setToolLogs] = useState<ToolLogIndexEntry[]>([]);
  const [activeToolLog, setActiveToolLog] = useState<ToolLogDetail | null>(null);
  const [sidebarSection, setSidebarSection] = useState<"sessions">("sessions");
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [projectMenuOpenKey, setProjectMenuOpenKey] = useState<string | null>(null);
  const [sessionMenuOpenKey, setSessionMenuOpenKey] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSectionKey>("configuration");
  const [settingsConfigScopes, setSettingsConfigScopes] = useState<SettingsConfigScope[]>([]);
  const [settingsConfigDrafts, setSettingsConfigDrafts] = useState<Record<string, string>>({});
  const [settingsMcpServers, setSettingsMcpServers] = useState<McpServerSummary[]>([]);
  const [settingsConfigScope, setSettingsConfigScope] = useState<SettingsConfigScopeKey>("project");
  const [settingsConfigSection, setSettingsConfigSection] = useState<SettingsConfigSectionKey>("provider");
  const [settingsConfigLoading, setSettingsConfigLoading] = useState(false);
  const [settingsConfigSaving, setSettingsConfigSaving] = useState(false);
  const [settingsConfigMessage, setSettingsConfigMessage] = useState("");
  const [windowMaximized, setWindowMaximized] = useState(false);
  const [contextPanelOpen, setContextPanelOpen] = useState(true);
  const [todoExpanded, setTodoExpanded] = useState(false);
  const [layout, setLayout] = useState<LayoutState>(() => readStoredLayout());
  const [layoutDragging, setLayoutDragging] = useState<LayoutDragState | null>(null);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modePickerOpen, setModePickerOpen] = useState(false);
  const [contextPopoverOpen, setContextPopoverOpen] = useState(false);
  const [taskGraphPanelOpen, setTaskGraphPanelOpen] = useState(false);
  const [archivedSessions, setArchivedSessions] = useState<ArchivedSessionsState>(() => readStoredArchivedSessions());
  const [selectedArchivedSessionKeys, setSelectedArchivedSessionKeys] = useState<string[]>([]);
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
  const contextPopoverRef = useRef<HTMLDivElement | null>(null);
  const projectMenuRef = useRef<HTMLDivElement | null>(null);
  const sessionMenuRef = useRef<HTMLDivElement | null>(null);
  const composerTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const conversationBodyRef = useRef<HTMLDivElement | null>(null);

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
    let cancelled = false;
    let unlisten: (() => void) | null = null;

    async function refreshWindowState() {
      try {
        const maximized = await isMainWindowMaximized();
        if (!cancelled) {
          setWindowMaximized(maximized);
        }
      } catch {
        if (!cancelled) {
          setWindowMaximized(false);
        }
      }
    }

    void refreshWindowState();
    void onMainWindowResized(() => {
      void refreshWindowState();
    }).then((nextUnlisten) => {
      if (cancelled) {
        nextUnlisten?.();
        return;
      }
      unlisten = nextUnlisten;
    });

    return () => {
      cancelled = true;
      unlisten?.();
    };
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
    if (!contextPopoverOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (!contextPopoverRef.current?.contains(event.target as Node)) {
        setContextPopoverOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setContextPopoverOpen(false);
      }
    }

    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [contextPopoverOpen]);

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

  useEffect(() => {
    if (!sessionMenuOpenKey) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (!sessionMenuRef.current?.contains(event.target as Node)) {
        setSessionMenuOpenKey(null);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setSessionMenuOpenKey(null);
      }
    }

    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [sessionMenuOpenKey]);

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
    const mention = currentPathMention(draft, composerCursor);
    const client = clientRef.current;
    if (!mention || !client) {
      setPathPickerOpen(false);
      setPathSuggestions([]);
      setSelectedPathIndex(0);
      return;
    }
    let cancelled = false;
    client
      .listWorkspacePaths(mention.query, 30)
      .then((paths) => {
        if (cancelled) {
          return;
        }
        setPathSuggestions(paths);
        setPathPickerOpen(paths.length > 0);
        setCommandPickerOpen(false);
        setSelectedPathIndex(0);
      })
      .catch(() => {
        if (cancelled) {
          return;
        }
        setPathSuggestions([]);
        setPathPickerOpen(false);
        setSelectedPathIndex(0);
      });
    return () => {
      cancelled = true;
    };
  }, [draft, composerCursor]);

  useEffect(() => {
    setTodoExpanded(false);
    setTaskGraphPanelOpen(false);
  }, [selectedSessionId]);

  useLayoutEffect(() => {
    const el = conversationBodyRef.current;
    if (el && selectedSessionId) {
      el.scrollTop = el.scrollHeight;
    }
  }, [selectedSessionId]);

  useEffect(() => {
    const projectPath = selectedProjectPath;
    const sessionId = selectedSessionId;
    if (!projectPath || !sessionId) {
      return;
    }
    void refreshTaskGraph(projectPath, sessionId);
  }, [selectedProjectPath, selectedSessionId]);

  useEffect(() => {
    const projectPath = selectedProjectPath;
    const sessionId = selectedSessionId;
    if (!projectPath || !sessionId) {
      return;
    }
    const hasActiveTurn = (activeProjectTurns[projectPath] ?? []).some((turn) => turn.sessionId === sessionId);
    if (!hasActiveTurn) {
      return;
    }
    let cancelled = false;
    async function refreshSelectedExecutionState() {
      const client = clientRef.current;
      if (!client || !projectPath || !sessionId) {
        return;
      }
      try {
        const [members, tasks] = await Promise.all([client.listActiveTeamMembers(sessionId), client.listTasks(sessionId)]);
        if (cancelled) {
          return;
        }
        const key = conversationStateKey(projectPath, sessionId) ?? "";
        setTeamActivity((previous) => ({
          ...previous,
          [key]: members,
        }));
        setTaskGraph((previous) => ({
          ...previous,
          [key]: tasks,
        }));
      } catch {
        if (!cancelled) {
          setTeamActivity((previous) => ({
            ...previous,
            [conversationStateKey(projectPath, sessionId) ?? ""]: [],
          }));
        }
      }
    }
    void refreshSelectedExecutionState();
    const interval = window.setInterval(() => void refreshSelectedExecutionState(), 1500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [activeProjectTurns, selectedProjectPath, selectedSessionId]);

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
            if (projectPathKey(projectPath) === projectPathKey(managedConnection.workspaceRoot)) {
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
      connectionState: "connected",
      connectionError: null,
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
    if (!client || !nextProject || !nextProject.status) {
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

    const visibleSessions = visibleSessionsForProject(projectPath, nextProject.sessions, archivedSessions);
    const nextSessionId =
      visibleSessions.find((session) => session.id === selectedSessionIdRef.current)?.id ?? visibleSessions[0]?.id ?? null;
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
      const visibleSessions = visibleSessionsForProject(projectPath, sortedSessions, archivedSessions);
      const project: ProjectState = {
        path: projectPath,
        label: getPathLeafName(projectPath),
        connection: managedConnection ?? {
          baseUrl: normalizedBaseUrl,
          wsUrl: runtimeStatus.ws_url,
          workspaceRoot: projectPath,
        },
        status: runtimeStatus,
        connectionState: "connected",
        connectionError: null,
        sessions: sortedSessions,
        pendingInteractions: interactionList,
        toolLogs: logList,
      };
      projectClientsRef.current[projectPath] = nextClient;
      setProjects((previous) => upsertProject(previous, project));
      setSelectedProjectPath(projectPath);
      setSessions(sortedSessions);
      const nextSessionId =
        visibleSessions.find((session) => session.id === selectedSessionIdRef.current)?.id ?? visibleSessions[0]?.id ?? null;
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
    setRuntimeConversationItems((previous) => {
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

  function clearPendingTurn(projectPath: string | null | undefined, sessionId: string | null | undefined, pendingTurnId: string) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    setPendingTurns((previous) => {
      const current = previous[key];
      if (!current || current.id !== pendingTurnId) {
        return previous;
      }
      const next = { ...previous };
      delete next[key];
      return next;
    });
  }

  function resetConversationRuntimeItems(projectPath: string | null | undefined, sessionId: string | null | undefined) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    setRuntimeConversationItems((previous) => ({ ...previous, [key]: [] }));
  }

  function appendAssistantRuntimeDelta(projectPath: string | null | undefined, sessionId: string | null | undefined, turnId: string | null | undefined, delta: string) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key || !delta) {
      return;
    }
    setRuntimeConversationItems((previous) => {
      const current = previous[key] ?? [];
      const last = current[current.length - 1];
      if (last?.type === "assistant_text") {
        return {
          ...previous,
          [key]: [...current.slice(0, -1), { ...last, text: `${last.text}${delta}` }],
        };
      }
      return {
        ...previous,
        [key]: [
          ...current,
          {
            id: runtimeItemId("assistant", turnId),
            type: "assistant_text",
            text: delta,
          },
        ],
      };
    });
  }

  function appendRuntimeUserMessage(
    projectPath: string | null | undefined,
    sessionId: string | null | undefined,
    turnId: string | null | undefined,
    text: string,
  ) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key || !text.trim()) {
      return;
    }
    setRuntimeConversationItems((previous) => ({
      ...previous,
      [key]: [
        ...(previous[key] ?? []),
        {
          id: runtimeItemId("user", turnId),
          type: "user_text",
          text,
        },
      ],
    }));
  }

  function appendRuntimeAssistantNotice(
    projectPath: string | null | undefined,
    sessionId: string | null | undefined,
    operationId: string | null | undefined,
    text: string,
  ) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key || !text.trim()) {
      return;
    }
    setRuntimeConversationItems((previous) => ({
      ...previous,
      [key]: [
        ...(previous[key] ?? []),
        {
          id: runtimeItemId("assistant", operationId),
          type: "assistant_text",
          text,
          isStreaming: false,
        },
      ],
    }));
  }

  function appendRuntimeToolStarted(projectPath: string | null | undefined, event: SidecarEvent) {
    const key = conversationStateKey(projectPath, event.session_id);
    if (!key) {
      return;
    }
    const toolName = readEventString(event.payload.tool_name, "tool");
    if (toolName === "subagent" && readEventString(event.payload.actor, "lead") === "lead") {
      noteSubagentStarted(projectPath, event);
    }
    setRuntimeConversationItems((previous) => ({
      ...previous,
      [key]: [
        ...(previous[key] ?? []),
        {
          id: runtimeItemId("tool", event.turn_id),
          type: "tool_call",
          toolCall: {
            id: runtimeItemId("tool-call", event.turn_id),
            name: toolName,
            input: stringifyToolValue(event.payload.tool_input ?? {}),
            output: "(running)",
            rawInput: event.payload.tool_input ?? {},
            rawOutput: null,
            logId: null,
            status: "running",
          },
        },
      ],
    }));
  }

  function applyRuntimeToolFinished(projectPath: string | null | undefined, event: SidecarEvent) {
    const key = conversationStateKey(projectPath, event.session_id);
    if (!key) {
      return;
    }
    const toolName = readEventString(event.payload.tool_name, "tool");
    if (toolName === "subagent" && readEventString(event.payload.actor, "lead") === "lead") {
      noteSubagentFinished(projectPath, event);
    }
    const finishedTool = {
      id: runtimeItemId("tool-call", event.turn_id),
      name: toolName,
      input: stringifyToolValue(event.payload.tool_input ?? {}),
      output: stringifyToolValue(event.payload.output ?? "(no output)"),
      rawInput: event.payload.tool_input ?? {},
      rawOutput: event.payload.output ?? "(no output)",
      contentBlocks: toolResultContentBlocksFromEvent(event.payload),
      logId: typeof event.payload.log_id === "string" ? event.payload.log_id : null,
      status: "finished" as const,
    };
    setRuntimeConversationItems((previous) => {
      const current = previous[key] ?? [];
      const matchIndex = findLastRunningToolIndex(current, toolName);
      if (matchIndex < 0) {
        return {
          ...previous,
          [key]: [...current, { id: runtimeItemId("tool", event.turn_id), type: "tool_call", toolCall: finishedTool }],
        };
      }
      return {
        ...previous,
        [key]: current.map((item, index) =>
          index === matchIndex && item.type === "tool_call"
            ? { ...item, toolCall: { ...item.toolCall, ...finishedTool, id: item.toolCall.id } }
            : item,
        ),
      };
    });
  }

  function noteSubagentStarted(projectPath: string | null | undefined, event: SidecarEvent) {
    const key = conversationStateKey(projectPath, event.session_id);
    if (!key) {
      return;
    }
    const toolInput = isRecord(event.payload.tool_input) ? event.payload.tool_input : {};
    const activityId = readEventString(event.payload.trace_id, `subagent-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    const prompt = readEventString(toolInput.prompt, "working");
    const agentType = readEventString(toolInput.agent_type, "Explore");
    setActiveSubagents((previous) => ({
      ...previous,
      [key]: {
        ...(previous[key] ?? {}),
        [activityId]: {
          id: activityId,
          prompt,
          agentType,
          startedAt: Date.now(),
          facts: [],
        },
      },
    }));
  }

  function noteSubagentActivity(projectPath: string | null | undefined, event: SidecarEvent) {
    const key = conversationStateKey(projectPath, event.session_id);
    if (!key) {
      return;
    }
    const payload = event.payload;
    const activityId = readEventString(payload.activity_id, "");
    const text = compactInlineText(readEventString(payload.text, ""), 180);
    if (!text) {
      return;
    }
    const prompt = readEventString(payload.prompt, "");
    const agentType = readEventString(payload.agent_type, "Explore");
    setActiveSubagents((previous) => {
      const current = previous[key] ?? {};
      const fallbackId = Object.keys(current).length === 1 ? Object.keys(current)[0] : "";
      const resolvedId = activityId && current[activityId] ? activityId : fallbackId || activityId;
      if (!resolvedId) {
        return previous;
      }
      const currentItem = current[resolvedId] ?? {
        id: resolvedId,
        prompt,
        agentType,
        startedAt: Date.now(),
        facts: [],
      };
      const facts = currentItem.facts[currentItem.facts.length - 1] === text ? currentItem.facts : [...currentItem.facts, text];
      return {
        ...previous,
        [key]: {
          ...current,
          [resolvedId]: {
            ...currentItem,
            prompt: currentItem.prompt || prompt,
            agentType: currentItem.agentType || agentType,
            facts: facts.slice(-SUBAGENT_FACTS_LIMIT),
            lastActivityAt: Date.now(),
          },
        },
      };
    });
  }

  function noteSubagentFinished(projectPath: string | null | undefined, event: SidecarEvent) {
    const key = conversationStateKey(projectPath, event.session_id);
    if (!key) {
      return;
    }
    const toolInput = isRecord(event.payload.tool_input) ? event.payload.tool_input : {};
    const prompt = readEventString(toolInput.prompt, "");
    const agentType = readEventString(toolInput.agent_type, "Explore");
    setActiveSubagents((previous) => {
      const current = previous[key] ?? {};
      const traceId = readEventString(event.payload.trace_id, "");
      let removeId = traceId && current[traceId] ? traceId : "";
      if (!removeId) {
        removeId =
          Object.values(current).find((item) => item.prompt === prompt && item.agentType === agentType)?.id ??
          (Object.keys(current).length === 1 ? Object.keys(current)[0] : "");
      }
      if (!removeId) {
        return previous;
      }
      const nextItems = { ...current };
      delete nextItems[removeId];
      const next = { ...previous };
      if (Object.keys(nextItems).length > 0) {
        next[key] = nextItems;
      } else {
        delete next[key];
      }
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

  function injectedUserMessageText(projectPath: string | null | undefined, sessionId: string, injectionId: string, payload: Record<string, unknown>): string {
    const text = typeof payload.text === "string" ? payload.text.trim() : "";
    if (text) {
      return text;
    }
    const key = conversationStateKey(projectPath, sessionId);
    const queuedPrompt = key ? (queuedPromptsRef.current[key] ?? []).find((prompt) => prompt.id === injectionId) : null;
    if (queuedPrompt?.userText.trim()) {
      return queuedPrompt.userText;
    }
    return buildOptimisticUserText("", []);
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
      const delta = typeof event.payload.delta === "string" ? event.payload.delta : "";
      if (delta) {
        appendAssistantRuntimeDelta(projectPath, event.session_id, event.turn_id, delta);
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
        clearConversationRuntimeState(projectPath, payloadSession.id);
      }
      return;
    }
    if (event.type === "session_deleted") {
      const sessionId = typeof event.payload.session_id === "string" ? event.payload.session_id : event.session_id;
      if (sessionId) {
        removeProjectSession(projectPath, sessionId);
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
        const injectedText = injectedUserMessageText(projectPath, event.session_id, injectionId, event.payload);
        appendRuntimeUserMessage(projectPath, event.session_id, event.turn_id, injectedText);
        removeQueuedPrompt(projectPath, event.session_id, injectionId);
      }
      return;
    }
    if (event.type === "tool_started") {
      appendRuntimeToolStarted(projectPath, event);
      return;
    }
    if (event.type === "tool_finished") {
      applyRuntimeToolFinished(projectPath, event);
      if (isActiveProject) {
        void refreshToolLogs();
      }
      const toolName = readEventString(event.payload.tool_name, "");
      if (toolName === "task_create" || toolName === "task_update" || toolName === "task_list" || toolName === "claim_task") {
        void refreshTaskGraph(projectPath, event.session_id ?? null);
      }
      return;
    }
    if (event.type === "subagent_activity") {
      noteSubagentActivity(projectPath, event);
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
      clearActivityState(projectPath, event.session_id);
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
      clearActivityState(projectPath, event.session_id);
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

  async function refreshTaskGraph(projectPath = selectedProjectPathRef.current, sessionId = selectedSessionIdRef.current) {
    const client = projectPath ? projectClientsRef.current[projectPath] ?? clientRef.current : clientRef.current;
    if (!client || !projectPath || !sessionId) {
      return;
    }
    try {
      const tasks = await client.listTasks(sessionId);
      const key = conversationStateKey(projectPath, sessionId);
      if (!key) {
        return;
      }
      setTaskGraph((previous) => ({
        ...previous,
        [key]: tasks,
      }));
    } catch {
      // Task graph is auxiliary UI; keep the conversation flow quiet on refresh failures.
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

  function removeProjectSession(projectPath: string | null, sessionId: string) {
    setSessions((previous) => previous.filter((session) => session.id !== sessionId));
    if (projectPath) {
      setProjects((previous) =>
        previous.map((project) =>
          project.path === projectPath ? { ...project, sessions: project.sessions.filter((session) => session.id !== sessionId) } : project,
        ),
      );
      setArchivedSessions((previous) => {
        const current = previous[projectPath] ?? [];
        if (!current.includes(sessionId)) {
          return previous;
        }
        const next = {
          ...previous,
          [projectPath]: current.filter((id) => id !== sessionId),
        };
        persistArchivedSessions(next);
        return next;
      });
    }
    if (selectedSessionIdRef.current === sessionId) {
      setSelectedSessionId(null);
      setCurrentSession(null);
      setActiveTurnId(null);
    }
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

  function clearActivityState(projectPath: string | null | undefined, sessionId: string | null | undefined) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    setActiveSubagents((previous) => {
      if (!previous[key]) {
        return previous;
      }
      const next = { ...previous };
      delete next[key];
      return next;
    });
    setTeamActivity((previous) => {
      if (!previous[key]) {
        return previous;
      }
      const next = { ...previous };
      delete next[key];
      return next;
    });
  }

  async function handleCreateProject() {
    setBusyAction("create-project");
    let projectPathForError: string | null = null;
    try {
      const projectPath = await chooseProjectFolder();
      if (!projectPath) {
        setBannerMessage("No project folder selected.");
        return;
      }
      projectPathForError = projectPath;
      const pendingProject: ProjectState = {
        path: projectPath,
        label: getPathLeafName(projectPath),
        connection: null,
        status: null,
        connectionState: "connecting",
        connectionError: null,
        sessions: [],
        pendingInteractions: [],
        toolLogs: [],
      };
      setProjects((previous) => upsertProject(previous, pendingProject));
      setSelectedProjectPath(projectPath);
      setSessions([]);
      setSelectedSessionId(null);
      setCurrentSession(null);
      setContextPanelOpen(true);
      setBannerMessage(`Adding project: ${projectPath}`);

      const managedConnection = await ensureManagedSidecar(projectPath);
      if (!managedConnection) {
        const message = "Project folder selection is only available in the desktop app.";
        markProjectConnectionError(projectPath, message);
        setBannerMessage(message);
        return;
      }
      await connectManagedProject(managedConnection, { selectProject: true });
      setBannerMessage(`Added project: ${managedConnection.workspaceRoot}`);
    } catch (error) {
      const message = formatErrorMessage(error);
      setBannerMessage(message);
      if (projectPathForError) {
        markProjectConnectionError(projectPathForError, message);
      }
    } finally {
      setBusyAction(null);
    }
  }

  function markProjectConnectionError(projectPath: string, message: string) {
    const projectKey = projectPathKey(projectPath);
    setProjects((previous) =>
      previous.map((project) =>
        projectPathKey(project.path) === projectKey
          ? {
              ...project,
              connectionState: "error",
              connectionError: message,
              status: null,
              sessions: [],
              pendingInteractions: [],
              toolLogs: [],
            }
          : project,
      ),
    );
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
      setActiveSubagents((previous) => removeProjectActivityKeys(previous, projectPath));
      setTeamActivity((previous) => removeProjectActivityKeys(previous, projectPath));
      setTaskGraph((previous) => removeProjectActivityKeys(previous, projectPath));

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
      resetConversationRuntimeItems(projectPath, session.id);
    }
    const userInput = await buildPromptPayload(client, prompt, images);
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
      await client.queueLoopInjection(activeTurn.turnId, prompt.id, await buildPromptPayload(client, prompt.prompt, prompt.images));
      setBannerMessage("Queued prompt will be injected on the next agent loop.");
    } catch (error) {
      updateQueuedPrompt(projectPath, prompt.sessionId, prompt.id, (current) => ({ ...current, injectionRequested: false }));
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleSendPrompt() {
    const commandTarget = pendingUiCommandTarget(draft, pendingImages);
    if (commandTarget) {
      openUiCommandTarget(commandTarget);
      return;
    }
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
      setPathPickerOpen(false);
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
      setPathPickerOpen(false);
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

  function handleComposerChange(value: string, cursor: number) {
    setDraft(value);
    setComposerCursor(cursor);
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
      setPathPickerOpen(false);
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
      setPathPickerOpen(false);
      composerTextareaRef.current?.focus();
    } catch (error) {
      setBannerMessage(`Unable to read selected image: ${formatErrorMessage(error)}`);
    }
  }

  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (pathPickerOpen && pathSuggestions.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSelectedPathIndex((current) => (current + 1) % pathSuggestions.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSelectedPathIndex((current) => (current - 1 + pathSuggestions.length) % pathSuggestions.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        applyPathSuggestion(pathSuggestions[selectedPathIndex] ?? pathSuggestions[0]);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setPathPickerOpen(false);
        return;
      }
    }

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
    const uiTarget = uiCommandTarget(command);
    if (uiTarget) {
      openUiCommandTarget(uiTarget);
      return;
    }
    setDraft(`${command} `);
    setCommandPickerOpen(false);
    setPathPickerOpen(false);
    requestAnimationFrame(() => {
      composerTextareaRef.current?.focus();
    });
  }

  function handleOpenSettings() {
    openConfigurationTarget("provider");
  }

  async function handleContextCommand(command: ContextCommandTarget) {
    const client = clientRef.current;
    const session = currentSessionRef.current;
    const projectPath = selectedProjectPathRef.current;
    const actionLabel = command === "compact" ? t("context.compactContext") : t("context.semanticJanitor");
    const placeholderText = command === "compact" ? t("context.compactContext") : t("context.semanticJanitor");
    if (!client || !session) {
      setBannerMessage("Select a session before changing context.");
      return;
    }
    if (projectPath && (activeProjectTurns[projectPath] ?? []).some((turn) => turn.sessionId === session.id)) {
      setBannerMessage("Wait for the active turn to finish before changing context.");
      return;
    }

    const operationId = `context-${command}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const key = conversationStateKey(projectPath, session.id);
    if (key) {
      setPendingTurns((previous) => ({
        ...previous,
        [key]: {
          id: operationId,
          sessionId: session.id,
          userText: "",
          placeholderText,
        },
      }));
      resetConversationRuntimeItems(projectPath, session.id);
    }
    if (projectPath) {
      setActiveProjectTurns((previous) => ({
        ...previous,
        [projectPath]: [
          ...(previous[projectPath] ?? []).filter((turn) => turn.sessionId !== session.id),
          { sessionId: session.id, turnId: operationId },
        ],
      }));
    }
    if (projectPath === selectedProjectPathRef.current && session.id === selectedSessionIdRef.current) {
      setActiveTurnId(operationId);
    }
    setBusyAction(`context-${command}`);
    setDraft("");
    setCommandPickerOpen(false);
    setPathPickerOpen(false);
    setHistoryCursor(null);
    setBannerMessage(`${actionLabel} started.`);
    try {
      const result = command === "compact" ? await client.compactSession(session.id) : await client.janitorSession(session.id);
      upsertProjectSession(projectPath, result.session);
      setCurrentSession(result.session);
      setContextPopoverOpen(false);
      clearPendingTurn(projectPath, session.id, operationId);
      appendRuntimeAssistantNotice(projectPath, session.id, operationId, result.message);
      setBannerMessage(result.message);
    } catch (error) {
      clearPendingTurn(projectPath, session.id, operationId);
      setBannerMessage(formatErrorMessage(error));
    } finally {
      clearPendingTurn(projectPath, session.id, operationId);
      if (projectPath) {
        clearActiveProjectTurn(projectPath, operationId);
      }
      if (session.id === selectedSessionIdRef.current) {
        setActiveTurnId((current) => (current === operationId ? null : current));
      }
      setBusyAction(null);
    }
  }

  function openUiCommandTarget(target: UiCommandTarget) {
    if (target.kind === "config") {
      openConfigurationTarget(target.target);
      return;
    }
    if (target.kind === "context") {
      void handleContextCommand(target.command);
      return;
    }
    setSettingsOpen(false);
    setDraft("");
    setCommandPickerOpen(false);
    setPathPickerOpen(false);
    setHistoryCursor(null);
    setModelPickerOpen(true);
    setModePickerOpen(false);
    requestAnimationFrame(() => {
      document.querySelector<HTMLElement>(".model-trigger")?.focus();
    });
  }

  function openConfigurationTarget(target: ConfigCommandTarget) {
    setSettingsOpen(true);
    setSettingsSection("configuration");
    setSelectedArchivedSessionKeys([]);
    if (target !== "skills") {
      setSettingsConfigSection(target);
    }
    setDraft("");
    setCommandPickerOpen(false);
    setPathPickerOpen(false);
    setHistoryCursor(null);
    setModelPickerOpen(false);
    setModePickerOpen(false);
    void refreshSettingsConfig();
    if (target === "skills") {
      requestAnimationFrame(() => {
        scrollSettingsPanelIntoView("skills");
      });
    }
  }

  function handleCloseSettings() {
    setSettingsOpen(false);
    setSelectedArchivedSessionKeys([]);
  }

  async function refreshSettingsConfig() {
    const client = clientRef.current;
    if (!client) {
      setSettingsConfigScopes([]);
      setSettingsConfigDrafts({});
      setSettingsMcpServers([]);
      setSettingsConfigMessage("Connect to a sidecar before editing configuration.");
      return;
    }
    setSettingsConfigLoading(true);
    try {
      const payload = await client.getSettingsConfig();
      const mcpServers = await client.listMcpServers();
      const nextDrafts: Record<string, string> = {};
      for (const scope of payload.scopes) {
        for (const section of ["provider", "mcp", "hooks", "system_prompt"] as SettingsConfigSectionKey[]) {
          nextDrafts[`${scope.scope}:${section}`] = scope.sections[section] ?? "";
        }
      }
      setSettingsConfigScopes(payload.scopes);
      setSettingsConfigDrafts(nextDrafts);
      setSettingsMcpServers(mcpServers);
      setSettingsConfigMessage("");
    } catch (error) {
      setSettingsConfigMessage(formatErrorMessage(error));
    } finally {
      setSettingsConfigLoading(false);
    }
  }

  async function handleSaveSettingsConfigSection() {
    const client = clientRef.current;
    if (!client) {
      setSettingsConfigMessage("Connect to a sidecar before saving configuration.");
      return;
    }
    const draftKey = `${settingsConfigScope}:${settingsConfigSection}`;
    setSettingsConfigSaving(true);
    try {
      const result = await client.saveSettingsConfigSection(
        settingsConfigScope,
        settingsConfigSection,
        settingsConfigDrafts[draftKey] ?? "",
      );
      await refreshSettingsConfig();
      setSettingsConfigMessage(
        result.runtime_reloaded
          ? `Saved ${result.section} to ${result.config_path}. Runtime MCP tools are active now.`
          : `Saved ${result.section} to ${result.config_path}. Restart the sidecar to apply runtime changes.`,
      );
    } catch (error) {
      setSettingsConfigMessage(formatErrorMessage(error));
    } finally {
      setSettingsConfigSaving(false);
    }
  }

  async function handleDebugMcpServer(serverName: string): Promise<number> {
    const client = clientRef.current;
    if (!client) {
      throw new Error("Connect to a sidecar before debugging MCP servers.");
    }
    const result = await client.debugMcpServer(serverName);
    setSettingsMcpServers((previous) => {
      const nextServer = result.server;
      if (!previous.some((server) => server.name === nextServer.name)) {
        return [...previous, nextServer];
      }
      return previous.map((server) => (server.name === nextServer.name ? nextServer : server));
    });
    return result.tool_count;
  }

  async function handleSetMcpServerEnabled(serverName: string, enabled: boolean): Promise<number> {
    const client = clientRef.current;
    if (!client) {
      throw new Error("Connect to a sidecar before changing MCP servers.");
    }
    const result = await client.setMcpServerEnabled(serverName, enabled);
    setSettingsMcpServers((previous) => {
      const nextServer = result.server;
      if (!previous.some((server) => server.name === nextServer.name)) {
        return [...previous, nextServer];
      }
      return previous.map((server) => (server.name === nextServer.name ? nextServer : server));
    });
    await refreshSettingsConfig();
    return result.tool_count;
  }

  async function handleOpenSettingsPath(path: string) {
    const targetPath = path.trim();
    if (!targetPath) {
      return;
    }
    try {
      await openWorkspaceRoot(targetPath);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleTitlebarPointerDown(event: ReactPointerEvent<HTMLElement>) {
    if (event.button !== 0 || event.detail > 1 || (event.target as HTMLElement).closest("button")) {
      return;
    }
    try {
      await startMainWindowDrag();
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleTitlebarDoubleClick(event: ReactMouseEvent<HTMLElement>) {
    if ((event.target as HTMLElement).closest("button")) {
      return;
    }
    try {
      await toggleMaximizeMainWindow();
      setWindowMaximized(await isMainWindowMaximized());
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleWindowControl(action: "minimize" | "toggle-maximize" | "close") {
    try {
      if (action === "minimize") {
        await minimizeMainWindow();
      } else if (action === "toggle-maximize") {
        await toggleMaximizeMainWindow();
        setWindowMaximized(await isMainWindowMaximized());
      } else {
        await closeMainWindow();
      }
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    }
  }

  function applyPathSuggestion(suggestion: WorkspacePathSuggestion) {
    const mention = currentPathMention(draft, composerCursor);
    if (!mention) {
      return;
    }
    const insertion = suggestion.kind === "dir" && !suggestion.path.endsWith("/") ? `${suggestion.path}/` : suggestion.path;
    const nextDraft = `${draft.slice(0, mention.queryStart)}${insertion}${draft.slice(mention.end)}`;
    const nextCursor = mention.queryStart + insertion.length;
    setDraft(nextDraft);
    setComposerCursor(nextCursor);
    setPathPickerOpen(false);
    setSelectedPathIndex(0);
    requestAnimationFrame(() => {
      const textarea = composerTextareaRef.current;
      textarea?.focus();
      textarea?.setSelectionRange(nextCursor, nextCursor);
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

  async function handleArchiveSession(projectPath: string, sessionId: string) {
    const nextArchivedSessions = appendArchivedSession(archivedSessions, projectPath, sessionId);
    setArchivedSessions(nextArchivedSessions);
    persistArchivedSessions(nextArchivedSessions);
    setSessionMenuOpenKey(null);

    if (selectedProjectPathRef.current === projectPath && selectedSessionIdRef.current === sessionId) {
      const project = projects.find((item) => item.path === projectPath);
      const remainingVisibleSessions = visibleSessionsForProject(
        projectPath,
        (project?.sessions ?? []).filter((session) => session.id !== sessionId),
        nextArchivedSessions,
      );
      const nextSession = remainingVisibleSessions[0] ?? null;
      if (nextSession) {
        await selectSession(nextSession.id, projectClientsRef.current[projectPath], project?.sessions, projectPath);
      } else {
        setSelectedSessionId(null);
        setCurrentSession(null);
        setActiveTurnId(null);
      }
    }

    setBannerMessage(`Archived session ${sessionId}.`);
  }

  function handleRestoreArchivedSessions(entries: ArchivedSessionEntry[]) {
    if (entries.length === 0) {
      return;
    }
    const nextArchivedSessions = restoreArchivedSessions(archivedSessions, entries);
    setArchivedSessions(nextArchivedSessions);
    persistArchivedSessions(nextArchivedSessions);
    setSelectedArchivedSessionKeys((previous) => previous.filter((key) => !entries.some((entry) => entry.key === key)));
    setBannerMessage(`Restored ${entries.length} archived session${entries.length === 1 ? "" : "s"}.`);
  }

  async function handleDeleteArchivedSessions(entries: ArchivedSessionEntry[]) {
    if (entries.length === 0) {
      return;
    }
    const confirmed =
      typeof window === "undefined" ||
      window.confirm(`Permanently delete ${entries.length} archived session${entries.length === 1 ? "" : "s"}? This cannot be undone.`);
    if (!confirmed) {
      return;
    }
    setBusyAction("delete-archived-sessions");
    try {
      for (const entry of entries) {
        const client = projectClientsRef.current[entry.projectPath];
        if (!client) {
          throw new Error(`Project client unavailable for ${entry.projectLabel}.`);
        }
        await client.deleteSession(entry.session.id);
        removeProjectSession(entry.projectPath, entry.session.id);
      }
      setSelectedArchivedSessionKeys((previous) => previous.filter((key) => !entries.some((entry) => entry.key === key)));
      setBannerMessage(`Deleted ${entries.length} archived session${entries.length === 1 ? "" : "s"} permanently.`);
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
  const activeRuntimeConversationItems = activeConversationKey ? runtimeConversationItems[activeConversationKey] ?? [] : [];
  const activeSubagentItems = activeConversationKey ? Object.values(activeSubagents[activeConversationKey] ?? {}) : [];
  const activeTeamItems = activeConversationKey ? teamActivity[activeConversationKey] ?? [] : [];
  const activeTaskItems = activeConversationKey ? taskGraph[activeConversationKey] ?? [] : [];
  const activeQueuedPrompts = activeConversationKey ? queuedPrompts[activeConversationKey] ?? [] : [];
  const conversationRows = buildConversationRows(currentSession, activeRuntimeConversationItems, activePendingTurn);
  const latestStreamingAssistantRowId =
    [...conversationRows].reverse().find((row) => row.role === "assistant" && row.isStreaming)?.id ?? null;
  const currentSessionInteraction = currentSession ? findSessionInteraction(pendingInteractions, currentSession.id) : null;
  const activeProjectTurnList = selectedProjectPath ? (activeProjectTurns[selectedProjectPath] ?? []) : [];
  const currentSessionTurn = currentSession ? activeProjectTurnList.find((turn) => turn.sessionId === currentSession.id) ?? null : null;
  const currentSessionRunning = currentSession ? activeProjectTurnList.some((turn) => turn.sessionId === currentSession.id) : false;
  const projectTurnLimitReached = activeProjectTurnList.length >= 2;
  const activeProviderLabel = status?.provider ?? selectedProvider ?? t("composer.provider");
  const activeModelLabel = status?.model ?? selectedModel ?? t("composer.model");
  const activeReasoningLabel = formatReasoningLevel(status?.reasoning_level ?? selectedReasoningLevel);
  const activeExecutionMode = normalizeExecutionMode(status?.execution_mode);
  const activeModeOption = EXECUTION_MODE_OPTIONS.find((mode) => mode.key === activeExecutionMode);
  const activeExecutionModeLabel =
    status?.execution_mode_title ?? (activeModeOption ? t(activeModeOption.titleKey) : t("common.executionModeUnavailable"));
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
    : t("ctx.usageUnavailable");
  const contextUsedLabel = contextUsage ? formatTokenCount(contextUsage.used_tokens) : "--";
  const contextWindowLabel = contextUsage?.max_tokens ? formatTokenCount(contextUsage.max_tokens) : "--";
  const contextRatioLabel = contextPercent === null ? "--" : `${contextPercent.toFixed(1)}%`;
  const commandSuggestions = currentCommandSuggestions(draft);
  const conversationPreview = currentSession ? buildSessionPreview(currentSession) : "";
  const conversationTitle = truncateTopic(conversationPreview || selectedSessionId || t("conversation.newConversation"));
  const todoSummary = currentSession ? buildTodoSummary(currentSession.todo_items) : null;
  const workspaceRootPath = status?.workspace_root ?? "";
  const workspaceRootName = workspaceRootPath ? getPathLeafName(workspaceRootPath) : t("common.workspace");
  const archivedSessionEntries = buildArchivedSessionEntries(projects, archivedSessions);
  const archivedSessionSelection = archivedSessionEntries.filter((entry) => selectedArchivedSessionKeys.includes(entry.key));
  const allArchivedSelected =
    archivedSessionEntries.length > 0 && archivedSessionEntries.every((entry) => selectedArchivedSessionKeys.includes(entry.key));
  const sessionProjectGroups = projects.map((project) => ({
    key: project.path,
    label: project.label,
    path: project.path,
    connectionState: project.connectionState,
    connectionError: project.connectionError ?? null,
    sessions: visibleSessionsForProject(project.path, project.sessions, archivedSessions),
    pendingInteractions: project.path === selectedProjectPath ? pendingInteractions : project.pendingInteractions,
  }));
  const visibleProjectCount = sessionProjectGroups.length;
  const workspaceStyle = {
    "--sidebar-width": `${layout.sidebarWidth}px`,
    "--context-width": `${layout.contextWidth}px`,
  } as CSSProperties;
  const maximizeTitle = windowMaximized ? t("titlebar.restore") : t("titlebar.maximize");
  return (
    <div className="shell">
      <header
        className="app-titlebar"
        data-tauri-drag-region
        onPointerDown={(event) => void handleTitlebarPointerDown(event)}
        onDoubleClick={(event) => void handleTitlebarDoubleClick(event)}
      >
        <div className="titlebar-brand" data-tauri-drag-region>
          <img className="titlebar-icon" src={appIconUrl} alt="" aria-hidden="true" data-tauri-drag-region />
          <span data-tauri-drag-region>{t("app.title")}</span>
        </div>
        <div className="titlebar-controls">
          <button className="titlebar-button" type="button" onClick={handleOpenSettings} title={t("settings.title")} aria-label={t("settings.title")}>
            ⚙
          </button>
          <button
            className="titlebar-button titlebar-minimize"
            type="button"
            onClick={() => void handleWindowControl("minimize")}
            title={t("titlebar.minimize")}
            aria-label={t("titlebar.minimize")}
          >
            <span aria-hidden="true" />
          </button>
          <button
            className={`titlebar-button ${windowMaximized ? "titlebar-restore" : "titlebar-maximize"}`}
            type="button"
            onClick={() => void handleWindowControl("toggle-maximize")}
            title={maximizeTitle}
            aria-label={maximizeTitle}
          >
            <span aria-hidden="true" />
          </button>
          <button
            className="titlebar-button close"
            type="button"
            onClick={() => void handleWindowControl("close")}
            title={t("titlebar.close")}
            aria-label={t("titlebar.close")}
          >
            ×
          </button>
        </div>
      </header>
      <div className="ambient ambient-left" />
      <div className="ambient ambient-right" />
      {settingsOpen ? (
        <SettingsView
          activeSection={settingsSection}
          onSelectSection={setSettingsSection}
          onClose={handleCloseSettings}
          archivedEntries={archivedSessionEntries}
          archivedSelection={archivedSessionSelection}
          selectedArchivedKeys={selectedArchivedSessionKeys}
          allArchivedSelected={allArchivedSelected}
          busy={busyAction !== null}
          onToggleArchivedSelection={(entryKey) =>
            setSelectedArchivedSessionKeys((previous) =>
              previous.includes(entryKey) ? previous.filter((key) => key !== entryKey) : [...previous, entryKey],
            )
          }
          onToggleSelectAllArchived={() =>
            setSelectedArchivedSessionKeys(allArchivedSelected ? [] : archivedSessionEntries.map((entry) => entry.key))
          }
          onSetArchivedSelection={setSelectedArchivedSessionKeys}
          onRestoreArchived={handleRestoreArchivedSessions}
          onDeleteArchived={handleDeleteArchivedSessions}
          onOpenPath={handleOpenSettingsPath}
          configScopes={settingsConfigScopes}
          configDrafts={settingsConfigDrafts}
          mcpServers={settingsMcpServers}
          selectedConfigScope={settingsConfigScope}
          selectedConfigSection={settingsConfigSection}
          configLoading={settingsConfigLoading}
          configSaving={settingsConfigSaving}
          configMessage={settingsConfigMessage}
          onSelectConfigScope={setSettingsConfigScope}
          onSelectConfigSection={setSettingsConfigSection}
          onConfigDraftChange={(key, value) => setSettingsConfigDrafts((previous) => ({ ...previous, [key]: value }))}
          onSaveConfigSection={handleSaveSettingsConfigSection}
          onDebugMcpServer={handleDebugMcpServer}
          onSetMcpServerEnabled={handleSetMcpServerEnabled}
          onReloadConfig={refreshSettingsConfig}
        />
      ) : null}
      <main
        ref={workspaceRef}
        className={`workspace ${contextPanelOpen ? "context-open" : "context-collapsed"} ${layoutDragging ? "resizing" : ""}`}
        style={workspaceStyle}
      >
        <aside className="panel sidebar-panel">
          <div className="panel-header">
            <div>
              <h2>{t("sidebar.projects")}</h2>
            </div>
            <div className="panel-header-actions">
              <span className="panel-count">{t("sidebar.total", { count: visibleProjectCount })}</span>
              <button
                className="action primary sidebar-new"
                onClick={() => void handleCreateProject()}
                disabled={busyAction !== null}
                title={t("sidebar.newProject")}
                aria-label={t("sidebar.newProject")}
              >
                +
              </button>
            </div>
          </div>

          <div className="session-list">
            {sessionProjectGroups.length === 0 ? (
              <div className="empty-card">
                <p>{t("sidebar.noProjects")}</p>
                <span>{t("sidebar.noProjectsHint")}</span>
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
                              {group.connectionState === "connecting" ? <em>{t("sidebar.connecting")}</em> : null}
                              {group.connectionState === "error" && group.connectionError ? <em>{group.connectionError}</em> : null}
                            </span>
                          </span>
                          <span className={`project-toggle-count ${group.connectionState}`}>{group.connectionState === "connected" ? group.sessions.length : "!"}</span>
                        </button>
                        <div className="project-menu" ref={projectMenuOpenKey === group.key ? projectMenuRef : null}>
                          <button
                            className="project-menu-trigger"
                            onClick={(event) => {
                              event.stopPropagation();
                              setProjectMenuOpenKey((current) => (current === group.key ? null : group.key));
                            }}
                            aria-label={`Project options for ${group.label}`}
                            title={t("sidebar.projectOptions")}
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
                                disabled={busyAction !== null || group.connectionState !== "connected"}
                              >
                                {t("sidebar.newSession")}
                              </button>
                              <button
                                className="project-menu-item danger"
                                onClick={() => {
                                  void handleRemoveProject(group.path);
                                }}
                                disabled={busyAction !== null}
                              >
                                {t("sidebar.removeProject")}
                              </button>
                            </div>
                          ) : null}
                        </div>
                      </div>
                      {isCollapsed ? null : (
                        <div className="project-session-list">
                          {group.connectionState === "connecting" ? <div className="project-status-card">{t("sidebar.startingSidecar")}</div> : null}
                          {group.connectionState === "error" && group.connectionError ? (
                            <div className="project-status-card error">{group.connectionError}</div>
                          ) : null}
                          {group.sessions.map((session) => {
                            const isSelected = selectedProjectPath === group.path && selectedSessionId === session.id;
                            const isAnswering = (activeProjectTurns[group.path] ?? []).some((turn) => turn.sessionId === session.id);
                            const isWaitingForDecision = group.pendingInteractions.some((interaction) => interaction.session_id === session.id);
                            const sessionMenuKey = `${group.path}::${session.id}`;
                            return (
                              <div
                                key={session.id}
                                className={`session-card ${isSelected ? "selected" : ""} ${isAnswering ? "answering" : ""} ${isWaitingForDecision ? "waiting-decision" : ""} ${sessionMenuOpenKey === sessionMenuKey ? "menu-open" : ""}`}
                              >
                                <button
                                  className="session-card-button"
                                  onClick={() => {
                                    setContextPanelOpen(true);
                                    void activateProject(group.path, projectClientsRef.current[group.path]).then(() =>
                                      selectSession(session.id, projectClientsRef.current[group.path], undefined, group.path),
                                    );
                                  }}
                                >
                                  <div className="session-card-head">
                                    <strong>{session.id}</strong>
                                  </div>
                                  <p>{buildSessionPreview(session)}</p>
                                  <span className="session-card-time">{formatRelativeTime(session.updated_at ?? session.created_at)}</span>
                                </button>
                                <div className="session-card-side">
                                  {isWaitingForDecision ? (
                                    <span className="session-decision-indicator" aria-label={t("sidebar.waitingDecision")} />
                                  ) : isAnswering ? (
                                    <span className="session-answering-indicator" aria-label={t("sidebar.agentResponding")}>
                                      <span aria-hidden="true" />
                                      <span aria-hidden="true" />
                                      <span aria-hidden="true" />
                                    </span>
                                  ) : (
                                    <div className="session-menu" ref={sessionMenuOpenKey === sessionMenuKey ? sessionMenuRef : null}>
                                      <button
                                        className="session-menu-trigger"
                                        onClick={(event) => {
                                          event.stopPropagation();
                                          setSessionMenuOpenKey((current) => (current === sessionMenuKey ? null : sessionMenuKey));
                                        }}
                                        aria-label={`Session options for ${session.id}`}
                                        title={t("sidebar.sessionOptions")}
                                      >
                                        ⋯
                                      </button>
                                      {sessionMenuOpenKey === sessionMenuKey ? (
                                        <div className="session-menu-panel">
                                          <button
                                            className="session-menu-item"
                                            onClick={() => void handleArchiveSession(group.path, session.id)}
                                            disabled={busyAction !== null}
                                          >
                                            {t("sidebar.archiveSession")}
                                          </button>
                                        </div>
                                      ) : null}
                                    </div>
                                  )}
                                </div>
                              </div>
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
          aria-label={t("sidebar.resizePanel")}
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
                title={workspaceRootPath || t("conversation.workspaceUnavailable")}
              >
                {workspaceRootName}
              </button>
            </div>
            <div className="status-cluster">
              <button
                className="action ghost detail-toggle"
                onClick={() => setContextPanelOpen((current) => !current)}
                title={contextPanelOpen ? t("conversation.hideDetails") : t("conversation.showDetails")}
                aria-label={contextPanelOpen ? t("conversation.hideDetails") : t("conversation.showDetails")}
              >
                ⋯
              </button>
            </div>
          </div>

          <TodoStatusBar summary={todoSummary} expanded={todoExpanded} onToggleExpanded={() => setTodoExpanded((current) => !current)} />

          <div ref={conversationBodyRef} className="conversation-body">
            {conversationRows.length === 0 && activeQueuedPrompts.length === 0 && !currentSessionInteraction ? (
              <div className="empty-conversation">
                <h3>{t("conversation.startSession")}</h3>
                <p>{t("conversation.startSessionHint")}</p>
              </div>
            ) : (
              conversationRows.map((row) => (
                <article key={row.id} className={`bubble ${row.role} ${row.isPending ? "pending" : ""}`}>
                  {row.parts?.length ? (
                    row.parts.map((part) =>
                      part.type === "text" ? (
                        <MarkdownMessage key={part.id} text={part.text} />
                      ) : (
                        <div key={part.id} className="tool-call-stack">
                          <ToolCallWithImages toolCall={part.toolCall} baseUrl={status?.base_url ?? clientRef.current?.baseUrl ?? ""} />
                        </div>
                      ),
                    )
                  ) : row.text ? (
                    <MarkdownMessage text={row.text} />
                  ) : null}
                  {row.isLoading ? (
                    <span className="typing-indicator" aria-label={t("conversation.waitingAssistant")}>
                      <span />
                      <span />
                      <span />
                    </span>
                  ) : null}
                  {!row.parts?.length && row.toolCalls?.length ? (
                    <div className="tool-call-stack">
                      {row.toolCalls.map((toolCall) => (
                        <ToolCallWithImages key={toolCall.id} toolCall={toolCall} baseUrl={status?.base_url ?? clientRef.current?.baseUrl ?? ""} />
                      ))}
                    </div>
                  ) : null}
                  {row.id === latestStreamingAssistantRowId ? (
                    <span className="session-answering-indicator conversation-answering-indicator" aria-label={t("sidebar.agentResponding")}>
                      <span aria-hidden="true" />
                      <span aria-hidden="true" />
                      <span aria-hidden="true" />
                    </span>
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
              onChange={(event) => handleComposerChange(event.target.value, event.target.selectionStart)}
              onKeyDown={handleComposerKeyDown}
              onKeyUp={(event) => setComposerCursor(event.currentTarget.selectionStart)}
              onSelect={(event) => setComposerCursor(event.currentTarget.selectionStart)}
              onClick={(event) => setComposerCursor(event.currentTarget.selectionStart)}
              onPaste={(event) => void handleComposerPaste(event)}
              placeholder={t("composer.placeholder")}
              disabled={busyAction !== null}
              rows={1}
            />
            {pendingImages.length > 0 ? (
              <div className="pending-attachments">
                {pendingImages.map((image) => (
                  <button
                    key={image.id}
                    className="pending-attachment"
                    onClick={() => removePendingImage(image.id)}
                    title={t("composer.removeImage", { name: image.name })}
                  >
                    <img className="pending-attachment-thumb" src={image.dataUrl} alt={image.name} />
                    <span className="pending-attachment-name">{image.name}</span>
                    <strong className="pending-attachment-remove">x</strong>
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
                    <span>{t(item.descriptionKey)}</span>
                  </button>
                ))}
              </div>
            ) : null}
            {pathPickerOpen && pathSuggestions.length > 0 ? (
              <div className="command-picker path-picker">
                {pathSuggestions.map((item, index) => (
                  <button
                    key={`${item.kind}-${item.path}`}
                    className={`command-option path-option ${index === selectedPathIndex ? "selected" : ""}`}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      applyPathSuggestion(item);
                    }}
                  >
                    <strong>{item.kind === "dir" ? `${item.path}/` : item.path}</strong>
                    <span>{item.kind === "dir" ? t("pathPicker.folder") : t("pathPicker.file")}</span>
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
                title={t("composer.attachImage")}
                aria-label={t("composer.attachImage")}
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
                            <span className="picker-label">{t("composer.provider")}</span>
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
                            <span className="picker-label">{t("composer.model")}</span>
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
                          <div className="reasoning-levels" role="group" aria-label={t("composer.reasoningLevel")}>
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
                            {t("composer.apply")}
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
                            <strong>{t(mode.titleKey)}</strong>
                            <span>{t(mode.descriptionKey)}</span>
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  <div className="ctx-popover-anchor" ref={contextPopoverRef}>
                    <button
                      type="button"
                      className={`ctx-meter ${contextPopoverOpen ? "open" : ""}`}
                      style={
                        {
                          "--ctx-color": contextColor,
                          "--ctx-fill": `${contextFill}%`,
                        } as CSSProperties
                      }
                      title={contextTitle}
                      aria-label={contextTitle}
                      aria-expanded={contextPopoverOpen}
                      onClick={() => setContextPopoverOpen((current) => !current)}
                      disabled={busyAction !== null}
                    >
                      <span className="ctx-ring" />
                      <span className="ctx-label">{contextLabel}</span>
                    </button>
                    {contextPopoverOpen ? (
                      <div className="ctx-popover" role="dialog" aria-label={t("ctx.windowDetails")}>
                        <div className="ctx-popover-header">
                          <strong>CTX</strong>
                          <span>{contextRatioLabel}</span>
                        </div>
                        <div className="ctx-popover-grid">
                          <span>{t("ctx.used")}</span>
                          <strong>{contextUsedLabel}</strong>
                          <span>{t("ctx.window")}</span>
                          <strong>{contextWindowLabel}</strong>
                          <span>{t("ctx.ratio")}</span>
                          <strong>{contextRatioLabel}</strong>
                        </div>
                        <div className="ctx-popover-actions">
                          <button
                            className="action secondary"
                            onClick={() => void handleContextCommand("compact")}
                            disabled={!currentSession || currentSessionRunning || busyAction !== null}
                          >
{t("context.compactContext")}
                          </button>
                          <button
                            className="action secondary"
                            onClick={() => void handleContextCommand("janitor")}
                            disabled={!currentSession || currentSessionRunning || busyAction !== null}
                          >
                            {t("context.semanticJanitor")}
                          </button>
                        </div>
                      </div>
                    ) : null}
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
                      ? t("composer.queueForSession")
                      : projectTurnLimitReached
                        ? t("composer.projectTurnLimit")
                        : t("composer.send")
                  }
                  aria-label={t("composer.send")}
                >
                  ↑
                </button>
                {currentSessionTurn ? (
                  <button
                    className="action danger composer-icon-action"
                    onClick={() => void handleInterrupt()}
                    disabled={busyAction !== null}
                    title={t("composer.interrupt")}
                    aria-label={t("composer.interrupt")}
                  >
                    ■
                  </button>
                ) : null}
              </div>
            </div>
          </div>
        </section>

        {taskGraphPanelOpen ? (
          <TaskGraphWorkspacePanel tasks={activeTaskItems} onClose={() => setTaskGraphPanelOpen(false)} />
        ) : null}

        {contextPanelOpen ? (
          <>
          <div
            className="layout-resizer context-resizer"
            role="separator"
            aria-label={t("context.resizePanel")}
            aria-orientation="vertical"
            onPointerDown={(event) => beginLayoutDrag("context", event)}
          />
          <aside className="panel context-panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">{t("context.kicker")}</p>
                <h2>{t("context.sessionDetails")}</h2>
              </div>
              <button className="action ghost" onClick={() => setContextPanelOpen(false)}>
                {t("context.collapse")}
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
                  <TaskGraphPanel tasks={activeTaskItems} onOpenPanel={() => setTaskGraphPanelOpen(true)} />
                  {activeSubagentItems.length > 0 || activeTeamItems.length > 0 ? (
                    <ExecutionActivityPanel subagents={activeSubagentItems} teamMembers={activeTeamItems} />
                  ) : null}
                  <div className="context-block">
                    <h3>Preview</h3>
                    <p>{buildSessionPreview(currentSession)}</p>
                  </div>
                </>
              ) : (
                <div className="empty-card">
                  <p>{t("context.noSession")}</p>
                  <span>{t("context.noSessionHint")}</span>
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

function TaskGraphPanel({ tasks, onOpenPanel }: { tasks: TaskGraphItem[]; onOpenPanel: () => void }) {
  const { t } = useI18n();
  const sortedTasks = [...tasks].sort((left, right) => Number(left.id) - Number(right.id));
  const counts = taskStatusCounts(sortedTasks);
  const graph = buildTaskGraphLayout(sortedTasks);
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const selectedTask = sortedTasks.find((task) => task.id === selectedTaskId) ?? null;

  return (
    <section className="task-graph-panel">
      <div className="task-graph-head">
        <div>
          <h3>{t("taskGraph.title")}</h3>
          <p>
            {counts.total === 0
              ? t("taskGraph.empty")
              : t("taskGraph.summary", { completed: counts.completed, total: counts.total, inProgress: counts.inProgress, pending: counts.pending })}
          </p>
        </div>
        <button className="settings-inline-button" type="button" onClick={onOpenPanel} disabled={sortedTasks.length === 0}>
          {t("taskGraph.expand")}
        </button>
      </div>
      {sortedTasks.length === 0 ? (
        <div className="task-graph-empty">{t("taskGraph.hint")}</div>
      ) : (
        <div className="task-graph-canvas">
          <TaskGraphSvg graph={graph} selectedTaskId={selectedTaskId} onSelectTask={setSelectedTaskId} compact />
        </div>
      )}
      {selectedTask ? <TaskGraphDetail task={selectedTask} onClose={() => setSelectedTaskId(null)} /> : null}
    </section>
  );
}

function TaskGraphWorkspacePanel({ tasks, onClose }: { tasks: TaskGraphItem[]; onClose: () => void }) {
  const { t } = useI18n();
  const sortedTasks = [...tasks].sort((left, right) => Number(left.id) - Number(right.id));
  const graph = buildTaskGraphLayout(sortedTasks, { expanded: true });
  const counts = taskStatusCounts(sortedTasks);
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const selectedTask = sortedTasks.find((task) => task.id === selectedTaskId) ?? null;

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <section className="task-graph-workspace-panel" role="dialog" aria-label={t("taskGraph.panelLabel")}>
      <div className="task-graph-workspace-head">
        <div>
          <h2>{t("taskGraph.title")}</h2>
          <p>{t("taskGraph.summary", { completed: counts.completed, total: counts.total, inProgress: counts.inProgress, pending: counts.pending })}</p>
        </div>
        <button className="settings-inline-button" type="button" onClick={onClose}>
          {t("taskGraph.close")}
        </button>
      </div>
      <div className="task-graph-workspace-canvas">
        <TaskGraphSvg graph={graph} selectedTaskId={selectedTaskId} onSelectTask={setSelectedTaskId} />
      </div>
      {selectedTask ? <TaskGraphDetail task={selectedTask} onClose={() => setSelectedTaskId(null)} /> : null}
    </section>
  );
}

function TaskGraphSvg({
  graph,
  selectedTaskId,
  onSelectTask,
  compact = false,
}: {
  graph: TaskGraphLayout;
  selectedTaskId: number | null;
  onSelectTask: (taskId: number) => void;
  compact?: boolean;
}) {
  return (
    <svg className={`task-graph-svg ${compact ? "compact" : ""}`} viewBox={`0 0 ${graph.width} ${graph.height}`} role="img" aria-label="Task dependency graph">
      <defs>
        <marker id={`task-arrow-${compact ? "compact" : "full"}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" />
        </marker>
      </defs>
      <g className="task-graph-edges">
        {graph.edges.map((edge) => (
          <path
            key={`${edge.from}-${edge.to}`}
            d={edgePath(edge, graph)}
            markerEnd={`url(#task-arrow-${compact ? "compact" : "full"})`}
          />
        ))}
      </g>
      <g className="task-graph-nodes">
        {graph.nodes.map((node) => (
          <g
            key={node.task.id}
            className={`task-graph-node ${taskStatus(node.task)} ${selectedTaskId === node.task.id ? "selected" : ""}`}
            transform={`translate(${node.x} ${node.y})`}
            role="button"
            tabIndex={0}
            onClick={() => onSelectTask(node.task.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelectTask(node.task.id);
              }
            }}
          >
            <rect width={graph.nodeWidth} height={graph.nodeHeight} rx="8" />
            <text className="task-graph-node-id" x="12" y="21">
              #{node.task.id}
            </text>
            <text className="task-graph-node-status" x={graph.nodeWidth - 12} y="21" textAnchor="end">
              {taskStatusLabel(taskStatus(node.task))}
            </text>
            <text className="task-graph-node-title" x="12" y="46">
              {svgLine(node.task.subject || "Untitled task", compact ? 24 : 32)}
            </text>
            <text className="task-graph-node-meta" x="12" y="70">
              {taskNodeMeta(node.task, compact ? 28 : 38)}
            </text>
          </g>
        ))}
      </g>
    </svg>
  );
}

function TaskGraphDetail({ task, onClose }: { task: TaskGraphItem; onClose: () => void }) {
  const { t } = useI18n();
  const blockedBy = task.blockedBy ?? [];
  const blocks = task.blocks ?? [];
  return (
    <section className="task-graph-detail">
      <div className="task-graph-detail-head">
        <div>
          <strong>#{task.id} · {task.subject || t("taskGraph.untitledTask")}</strong>
          <span>{taskStatusLabel(taskStatus(task))}</span>
        </div>
        <button className="settings-inline-button" type="button" onClick={onClose}>
          {t("taskGraph.close")}
        </button>
      </div>
      {task.description ? <p>{task.description}</p> : null}
      <dl>
        <div>
          <dt>{t("taskGraph.owner")}</dt>
          <dd>{task.owner || t("taskGraph.unassigned")}</dd>
        </div>
        <div>
          <dt>{t("taskGraph.preferred")}</dt>
          <dd>{task.preferred_owner || t("taskGraph.none")}</dd>
        </div>
        <div>
          <dt>{t("taskGraph.blockedBy")}</dt>
          <dd>{blockedBy.length ? blockedBy.map((id) => `#${id}`).join(", ") : t("taskGraph.none")}</dd>
        </div>
        <div>
          <dt>{t("taskGraph.blocks")}</dt>
          <dd>{blocks.length ? blocks.map((id) => `#${id}`).join(", ") : t("taskGraph.none")}</dd>
        </div>
      </dl>
    </section>
  );
}

function ExecutionActivityPanel({
  subagents,
  teamMembers,
}: {
  subagents: SubagentActivity[];
  teamMembers: TeamMemberActivity[];
}) {
  const { t } = useI18n();
  return (
    <section className="activity-panel" aria-live="polite">
      <div className="activity-panel-head">
        <span className="activity-pulse" aria-hidden="true" />
        <h3>{t("activity.executionActivity")}</h3>
      </div>
      {subagents.length > 0 ? (
        <div className="activity-group">
          <strong>{t("activity.subagents")}</strong>
          {subagents.map((item) => (
            <div key={item.id} className="activity-item">
              <div className="activity-item-head">
                <span>{item.agentType}</span>
                <em>{formatElapsedSeconds(item.startedAt)}</em>
              </div>
              <p>{compactInlineText(item.prompt || t("common.working"), 120)}</p>
              {item.facts.length > 0 ? <small>{item.facts[item.facts.length - 1]}</small> : null}
            </div>
          ))}
        </div>
      ) : null}
      {teamMembers.length > 0 ? (
        <div className="activity-group">
          <strong>{t("activity.agentTeam")}</strong>
          {teamMembers.map((member) => {
            const interactions = Array.isArray(member.recent_interactions) ? member.recent_interactions.filter(Boolean) : [];
            return (
              <div key={String(member.name)} className="activity-item">
                <div className="activity-item-head">
                  <span>{member.name}</span>
                  <em>{member.status ?? t("common.active")}</em>
                </div>
                <p>{teamMemberSummary(member)}</p>
                {interactions.length > 0 ? <small>{interactions[interactions.length - 1]}</small> : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
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
  const { t } = useI18n();
  const isAuthorization = interaction.kind === "authorization";
  return (
    <section className="decision-card" aria-live="polite">
      <div className="decision-copy">
        <p className="eyebrow">{isAuthorization ? t("decision.authorizationRequest") : t("decision.modeSwitchRequest")}</p>
        <h3>{interactionTitle(interaction, t)}</h3>
        <p>{interactionSummary(interaction, t)}</p>
      </div>
      {isAuthorization ? (
        <div className="decision-actions">
          <button
            className="action primary"
            onClick={() => void onResolveAuthorization(interaction.id, "once", true, t("decision.allowOnceReason"))}
            disabled={busy}
          >
            {t("decision.allowOnce")}
          </button>
          <button
            className="action secondary"
            onClick={() => void onResolveAuthorization(interaction.id, "workspace", true, t("decision.allowWorkspaceReason"))}
            disabled={busy}
          >
            {t("decision.allowWorkspace")}
          </button>
          <button
            className="action danger"
            onClick={() => void onResolveAuthorization(interaction.id, "deny", false, t("decision.denyReason"))}
            disabled={busy}
          >
            {t("decision.deny")}
          </button>
        </div>
      ) : (
        <div className="decision-actions">
          <button className="action primary" onClick={() => void onResolveModeSwitch(interaction, true)} disabled={busy}>
            {t("decision.switchNow")}
          </button>
          <button className="action danger" onClick={() => void onResolveModeSwitch(interaction, false)} disabled={busy}>
            {t("decision.stayHere")}
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
  const { t } = useI18n();
  return (
    <section className="prompt-queue-card" aria-live="polite">
      <div className="prompt-queue-head">
        <p className="eyebrow">{t("queue.queuedPrompts")}</p>
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
              title={prompt.injectionRequested ? t("queue.waitingNextLoop") : t("queue.injectOnNextLoop")}
            >
              {prompt.injectionRequested ? t("queue.nextLoop") : t("queue.injectNextLoop")}
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

function MermaidDiagram({ source }: { source: string }) {
  const { t } = useI18n();
  const [svg, setSvg] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"graph" | "code">("graph");
  const [fullscreenOpen, setFullscreenOpen] = useState(false);
  const [fullscreenZoom, setFullscreenZoom] = useState(1);
  const [fullscreenPan, setFullscreenPan] = useState({ x: 0, y: 0 });
  const [fullscreenDragging, setFullscreenDragging] = useState(false);
  const fullscreenDragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const renderId = `somnia-mermaid-${Date.now()}-${mermaidRenderCounter++}`;

    setSvg("");
    setError(null);
    void (async () => {
      try {
        const result = await mermaid.render(renderId, source);
        if (!cancelled) {
          setSvg(result.svg);
        }
      } catch (renderError) {
        if (!cancelled) {
          setError(formatErrorMessage(renderError));
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [source]);

  useEffect(() => {
    if (!fullscreenOpen) {
      return;
    }

    setFullscreenZoom(1);
    setFullscreenPan({ x: 0, y: 0 });

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setFullscreenOpen(false);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [fullscreenOpen]);

  function adjustFullscreenZoom(delta: number) {
    setFullscreenZoom((current) => clampMermaidZoom(current + delta));
  }

  function resetFullscreenView() {
    setFullscreenZoom(1);
    setFullscreenPan({ x: 0, y: 0 });
  }

  function handleFullscreenPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) {
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    fullscreenDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: fullscreenPan.x,
      originY: fullscreenPan.y,
    };
    setFullscreenDragging(true);
  }

  function handleFullscreenPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = fullscreenDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) {
      return;
    }
    setFullscreenPan({
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY,
    });
  }

  function handleFullscreenPointerEnd(event: ReactPointerEvent<HTMLDivElement>) {
    if (fullscreenDragRef.current?.pointerId !== event.pointerId) {
      return;
    }
    fullscreenDragRef.current = null;
    setFullscreenDragging(false);
  }

  if (error) {
    return (
      <div className="mermaid-card mermaid-card-error">
        <div className="mermaid-card-head">
          <span>{t("mermaid.title")}</span>
          <span>{t("mermaid.renderFailed")}</span>
        </div>
        <pre className="markdown-code-block">
          <span className="markdown-code-language">mermaid</span>
          <code>{source}</code>
        </pre>
        <p>{error}</p>
      </div>
    );
  }

  return (
    <>
      <figure className="mermaid-card">
        <figcaption className="mermaid-card-head">
          <span>{t("mermaid.title")}</span>
          <span className="mermaid-card-actions">
            <button type="button" className={viewMode === "graph" ? "active" : ""} onClick={() => setViewMode("graph")}>
              {t("mermaid.graph")}
            </button>
            <button type="button" className={viewMode === "code" ? "active" : ""} onClick={() => setViewMode("code")}>
              {t("mermaid.code")}
            </button>
            <button type="button" onClick={() => setFullscreenOpen(true)} disabled={!svg || viewMode !== "graph"}>
              {t("mermaid.fullscreen")}
            </button>
          </span>
        </figcaption>
        {viewMode === "code" ? (
          <pre className="markdown-code-block mermaid-source-block">
            <span className="markdown-code-language">mermaid</span>
            <code>{source}</code>
          </pre>
        ) : (
          <button
            type="button"
            className="mermaid-canvas"
            onClick={() => {
              if (svg) {
                setFullscreenOpen(true);
              }
            }}
            disabled={!svg}
            aria-label={t("mermaid.openFullscreen")}
          >
            {svg ? <span dangerouslySetInnerHTML={{ __html: svg }} /> : <span className="mermaid-loading">{t("mermaid.rendering")}</span>}
          </button>
        )}
      </figure>
      {fullscreenOpen && svg ? (
        <div className="mermaid-fullscreen" role="dialog" aria-modal="true" aria-label={t("mermaid.diagram")}>
          <div className="mermaid-fullscreen-head">
            <span>{t("mermaid.diagram")}</span>
            <span className="mermaid-fullscreen-actions">
              <button type="button" onClick={() => adjustFullscreenZoom(-MERMAID_ZOOM_STEP)}>
                -
              </button>
              <span>{Math.round(fullscreenZoom * 100)}%</span>
              <button type="button" onClick={() => adjustFullscreenZoom(MERMAID_ZOOM_STEP)}>
                +
              </button>
              <button type="button" onClick={resetFullscreenView}>
                {t("mermaid.reset")}
              </button>
              <button type="button" onClick={() => setFullscreenOpen(false)}>
                {t("mermaid.close")}
              </button>
            </span>
          </div>
          <button
            type="button"
            className="mermaid-fullscreen-backdrop"
            aria-label={t("mermaid.closeFullscreen")}
            onClick={() => setFullscreenOpen(false)}
          />
          <div
            className={`mermaid-fullscreen-canvas${fullscreenDragging ? " is-dragging" : ""}`}
            onClick={(event) => event.stopPropagation()}
            onPointerDown={handleFullscreenPointerDown}
            onPointerMove={handleFullscreenPointerMove}
            onPointerUp={handleFullscreenPointerEnd}
            onPointerCancel={handleFullscreenPointerEnd}
            onWheel={(event) => {
              event.preventDefault();
              adjustFullscreenZoom(event.deltaY < 0 ? MERMAID_ZOOM_STEP : -MERMAID_ZOOM_STEP);
            }}
          >
            <span
              className="mermaid-fullscreen-graph"
              style={{
                transform: `translate(${fullscreenPan.x}px, ${fullscreenPan.y}px) scale(${fullscreenZoom})`,
              }}
              dangerouslySetInnerHTML={{ __html: svg }}
            />
          </div>
        </div>
      ) : null}
    </>
  );
}

function clampMermaidZoom(value: number): number {
  return Math.min(MERMAID_MAX_ZOOM, Math.max(MERMAID_MIN_ZOOM, Number(value.toFixed(2))));
}

function ToolCallWithImages({ toolCall, baseUrl }: { toolCall: ConversationToolCall; baseUrl: string }) {
  const imageReferences = toolCall.contentBlocks?.filter((block) => block.type === "image_reference") ?? [];
  return (
    <>
      <ToolCallCard toolCall={toolCall} />
      {imageReferences.length > 0 ? (
        <div className="tool-image-list">
          {imageReferences.map((image, index) => (
            <ToolImagePreview key={`${image.path ?? image.absolute_path ?? image.image_url ?? "image"}-${index}`} image={image} baseUrl={baseUrl} />
          ))}
        </div>
      ) : null}
    </>
  );
}

function ToolCallCard({ toolCall }: { toolCall: ConversationToolCall }) {
  const { t } = useI18n();
  const resultState = toolCallResultState(toolCall);
  const fileChange = fileChangeSummary(toolCall, resultState, t);
  return (
    <details className={`tool-call-card ${resultState}`}>
      <summary>
        <span className="tool-call-summary-main">
          <span className={`tool-result-dot ${resultState}`} aria-hidden="true" />
          <span>{fileChange ? fileChange.actionLabel : toolCall.name}</span>
          {fileChange ? <em>{fileChange.path}</em> : null}
        </span>
        <span className="tool-call-summary-meta">
          {fileChange ? (
            <>
              <span className="file-change-stat added">+{fileChange.added}</span>
              <span className="file-change-stat removed">-{fileChange.removed}</span>
            </>
          ) : null}
          {toolCall.logId ? <em>{toolCall.logId}</em> : null}
        </span>
      </summary>
      {fileChange ? (
        <FileChangeDetail change={fileChange} />
      ) : (
        <>
          <div className="tool-call-detail">
            <span>{t("toolCall.input")}</span>
            <pre>{toolCall.input}</pre>
          </div>
          <div className="tool-call-detail">
            <span>{t("toolCall.output")}</span>
            <pre>{toolCall.output}</pre>
          </div>
        </>
      )}
    </details>
  );
}

function ToolImagePreview({
  image,
  baseUrl,
}: {
  image: NonNullable<ConversationToolCall["contentBlocks"]>[number] & { type: "image_reference" };
  baseUrl: string;
}) {
  const src = toolImageSource(image, baseUrl);
  const label = image.path || image.absolute_path || image.image_url || "tool image";
  if (!src) {
    return <span className="tool-image-missing">{label}</span>;
  }
  return (
    <a className="tool-image-preview" href={src} target="_blank" rel="noreferrer" title={label}>
      <img src={src} alt={label} loading="lazy" />
      <span>{label}</span>
    </a>
  );
}

function toolImageSource(image: { path?: string; absolute_path?: string; image_url?: string }, baseUrl: string): string {
  const imageUrl = String(image.image_url ?? "").trim();
  if (/^(?:https?:|data:image\/)/i.test(imageUrl)) {
    return imageUrl;
  }
  const path = String(image.path || image.absolute_path || "").trim();
  if (!path || !baseUrl.trim()) {
    return "";
  }
  const normalizedBaseUrl = normalizeBaseUrl(baseUrl);
  return `${normalizedBaseUrl}/workspace/images?path=${encodeURIComponent(path)}`;
}

function toolResultContentBlocksFromEvent(payload: Record<string, unknown>): ConversationContentBlock[] {
  if (!Array.isArray(payload.content_blocks)) {
    return [];
  }
  const blocks: ConversationContentBlock[] = [];
  for (const item of payload.content_blocks) {
    if (!isRecord(item)) {
      continue;
    }
    if (item.type === "text") {
      blocks.push({ type: "text", text: String(item.text ?? "") });
      continue;
    }
    if (item.type === "image_reference") {
      blocks.push({
        type: "image_reference",
        path: typeof item.path === "string" ? item.path : undefined,
        absolute_path: typeof item.absolute_path === "string" ? item.absolute_path : undefined,
        media_type: typeof item.media_type === "string" ? item.media_type : undefined,
        image_url: typeof item.image_url === "string" ? item.image_url : undefined,
        origin: typeof item.origin === "string" ? item.origin : undefined,
      });
    }
  }
  return blocks;
}

type ToolResultState = "running" | "success" | "error";

type DiffLine = {
  key: string;
  kind: "context" | "added" | "removed" | "meta";
  oldLine: number | null;
  newLine: number | null;
  text: string;
};

type FileChangeSummary = {
  actionLabel: string;
  path: string;
  added: number;
  removed: number;
  diffLines: DiffLine[];
};

function FileChangeDetail({ change }: { change: FileChangeSummary }) {
  const { t } = useI18n();
  return (
    <div className="tool-call-detail file-change-detail">
      <span>{t("toolCall.changes")}</span>
      <pre className="file-diff-view" aria-label={t("toolCall.changesFor", { path: change.path })}>
        {change.diffLines.length > 0 ? (
          change.diffLines.map((line) => (
            <span key={line.key} className={`file-diff-line ${line.kind}`}>
              <span className="file-diff-line-number">{line.newLine ?? line.oldLine ?? ""}</span>
              <span className="file-diff-marker">{diffMarker(line.kind)}</span>
              <code>{line.text || " "}</code>
            </span>
          ))
        ) : (
          <span className="file-diff-line context">
            <span className="file-diff-line-number" />
            <span className="file-diff-marker" />
            <code>{t("toolCall.fileUpdated")}</code>
          </span>
        )}
      </pre>
    </div>
  );
}

function diffMarker(kind: DiffLine["kind"]): string {
  if (kind === "added") {
    return "+";
  }
  if (kind === "removed") {
    return "-";
  }
  return " ";
}

function toolCallResultState(toolCall: ConversationToolCall): ToolResultState {
  if (toolCall.status === "running") {
    return "running";
  }
  const output = toolCall.rawOutput;
  if (typeof output === "string") {
    const lowered = output.trim().toLowerCase();
    return lowered.startsWith("error:") || lowered.startsWith("unknown tool:") || lowered.startsWith("blocked in ") ? "error" : "success";
  }
  if (!isRecord(output)) {
    return "success";
  }
  const status = String(output.status ?? "").trim().toLowerCase();
  if (["error", "failed", "denied"].includes(status)) {
    return "error";
  }
  if (output.success === false || output.isError === true) {
    return "error";
  }
  const error = output.error;
  if (typeof error === "string" && error.trim()) {
    return "error";
  }
  return "success";
}

function fileChangeSummary(toolCall: ConversationToolCall, resultState: ToolResultState, t: (key: import("./lib/i18n").TranslationKey, params?: Record<string, string | number>) => string): FileChangeSummary | null {
  if (resultState !== "success" || !isRecord(toolCall.rawOutput)) {
    return null;
  }
  const action = String(toolCall.rawOutput.action ?? toolCall.name).trim();
  if (!["write_file", "edit_file", "delete_file"].includes(toolCall.name) && !["write_file", "edit_file", "delete_file"].includes(action)) {
    return null;
  }
  if (toolCall.name === "delete_file" || action === "delete_file") {
    const path = String(toolCall.rawOutput.path ?? readablePathFromInput(toolCall.rawInput) ?? t("common.unknownPath"));
    return {
      actionLabel: t("toolCall.delete"),
      path,
      added: 0,
      removed: 0,
      diffLines: [
        {
          key: "delete",
          kind: "removed",
          oldLine: null,
          newLine: null,
          text: path,
        },
      ],
    };
  }
  const path = String(toolCall.rawOutput.path ?? readablePathFromInput(toolCall.rawInput) ?? t("common.unknownPath"));
  const added = numberFromValue(toolCall.rawOutput.added_lines);
  const removed = numberFromValue(toolCall.rawOutput.removed_lines);
  const actionLabel = toolCall.name === "write_file" || action === "write_file" ? (toolCall.rawOutput.existed_before === false ? t("toolCall.create") : t("toolCall.write")) : t("toolCall.update");
  return {
    actionLabel,
    path,
    added,
    removed,
    diffLines: structuredDiffLines(toolCall.rawOutput.diff_hunks) ?? buildFileDiffLines(toolCall.name || action, toolCall.rawInput),
  };
}

function structuredDiffLines(value: unknown): DiffLine[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const lines: DiffLine[] = [];
  value.forEach((item, index) => {
    if (!isRecord(item)) {
      return;
    }
    const rawKind = String(item.kind ?? "context").trim();
    const kind: DiffLine["kind"] =
      rawKind === "added" || rawKind === "removed" || rawKind === "meta" || rawKind === "context" ? rawKind : "context";
    lines.push({
      key: `structured-${index}`,
      kind,
      oldLine: nullableNumberFromValue(item.old_line),
      newLine: nullableNumberFromValue(item.new_line),
      text: String(item.text ?? ""),
    });
  });
  return lines;
}

function buildFileDiffLines(toolName: string, rawInput: unknown): DiffLine[] {
  if (!isRecord(rawInput)) {
    return [];
  }
  if (toolName === "edit_file") {
    return buildEditFileDiffLines(rawInput);
  }
  if (toolName === "write_file") {
    const content = String(rawInput.content ?? "");
    return renderUnifiedTextDiff("", content);
  }
  return [];
}

function buildEditFileDiffLines(rawInput: Record<string, unknown>): DiffLine[] {
  const edits = Array.isArray(rawInput.edits) ? rawInput.edits : [];
  const lines: DiffLine[] = [];
  edits.forEach((item, editIndex) => {
    if (!isRecord(item)) {
      return;
    }
    const oldText = String(item.old_text ?? "");
    const newText = String(item.new_text ?? "");
    if (!oldText && !newText) {
      return;
    }
    if (edits.length > 1) {
      const path = String(item.path ?? rawInput.path ?? "").trim();
      lines.push({
        key: `edit-${editIndex}-meta`,
        kind: "meta",
        oldLine: null,
        newLine: null,
        text: path ? `edit ${editIndex + 1}: ${path}` : `edit ${editIndex + 1}`,
      });
    }
    lines.push(...renderUnifiedTextDiff(oldText, newText, `edit-${editIndex}`));
  });
  return lines;
}

function renderUnifiedTextDiff(before: string, after: string, keyPrefix = "diff"): DiffLine[] {
  const beforeLines = before.replace(/\r\n?/g, "\n").split("\n");
  const afterLines = after.replace(/\r\n?/g, "\n").split("\n");
  if (beforeLines.length === 1 && beforeLines[0] === "") {
    beforeLines.length = 0;
  }
  if (afterLines.length === 1 && afterLines[0] === "") {
    afterLines.length = 0;
  }
  const prefixCount = commonPrefixLineCount(beforeLines, afterLines);
  const suffixCount = commonSuffixLineCount(beforeLines, afterLines, prefixCount);
  const visibleBeforeStart = Math.max(0, prefixCount - 2);
  const visibleAfterStart = Math.max(0, prefixCount - 2);
  const visibleBeforeEnd = Math.min(beforeLines.length, beforeLines.length - suffixCount + 2);
  const visibleAfterEnd = Math.min(afterLines.length, afterLines.length - suffixCount + 2);
  const lines: DiffLine[] = [];
  let oldLine = visibleBeforeStart + 1;
  let newLine = visibleAfterStart + 1;
  for (let index = visibleBeforeStart; index < prefixCount; index += 1) {
    lines.push({ key: `${keyPrefix}-ctx-before-${index}`, kind: "context", oldLine: oldLine++, newLine: newLine++, text: beforeLines[index] ?? "" });
  }
  for (let index = prefixCount; index < beforeLines.length - suffixCount; index += 1) {
    lines.push({ key: `${keyPrefix}-removed-${index}`, kind: "removed", oldLine: oldLine++, newLine: null, text: beforeLines[index] ?? "" });
  }
  for (let index = prefixCount; index < afterLines.length - suffixCount; index += 1) {
    lines.push({ key: `${keyPrefix}-added-${index}`, kind: "added", oldLine: null, newLine: newLine++, text: afterLines[index] ?? "" });
  }
  const suffixStartBefore = Math.max(prefixCount, beforeLines.length - suffixCount);
  const suffixStartAfter = Math.max(prefixCount, afterLines.length - suffixCount);
  oldLine = suffixStartBefore + 1;
  newLine = suffixStartAfter + 1;
  for (let index = 0; index < Math.min(2, suffixCount); index += 1) {
    const beforeIndex = suffixStartBefore + index;
    const afterIndex = suffixStartAfter + index;
    if (beforeIndex >= visibleBeforeEnd || afterIndex >= visibleAfterEnd) {
      break;
    }
    lines.push({
      key: `${keyPrefix}-ctx-after-${index}`,
      kind: "context",
      oldLine: oldLine++,
      newLine: newLine++,
      text: beforeLines[beforeIndex] ?? afterLines[afterIndex] ?? "",
    });
  }
  return lines;
}

function commonPrefixLineCount(left: string[], right: string[]): number {
  const limit = Math.min(left.length, right.length);
  let index = 0;
  while (index < limit && left[index] === right[index]) {
    index += 1;
  }
  return index;
}

function commonSuffixLineCount(left: string[], right: string[], prefixCount: number): number {
  const limit = Math.min(left.length, right.length) - prefixCount;
  let count = 0;
  while (count < limit && left[left.length - 1 - count] === right[right.length - 1 - count]) {
    count += 1;
  }
  return count;
}

function readablePathFromInput(input: unknown): string | null {
  if (!isRecord(input)) {
    return null;
  }
  if (typeof input.path === "string" && input.path.trim()) {
    return input.path;
  }
  const edits = Array.isArray(input.edits) ? input.edits : [];
  for (const item of edits) {
    if (isRecord(item) && typeof item.path === "string" && item.path.trim()) {
      return item.path;
    }
  }
  return null;
}

function numberFromValue(value: unknown): number {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? Math.max(0, Math.trunc(numberValue)) : 0;
}

function nullableNumberFromValue(value: unknown): number | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? Math.max(0, Math.trunc(numberValue)) : null;
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
      const code = codeLines.join("\n");
      if (/^mermaid\b/i.test(language)) {
        blocks.push(<MermaidDiagram key={`block-${key++}`} source={code} />);
      } else {
        blocks.push(
          <pre key={`block-${key++}`} className="markdown-code-block">
            {language ? <span className="markdown-code-language">{language}</span> : null}
            <code>{code}</code>
          </pre>,
        );
      }
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

    if (isMarkdownTableHeader(line, lines[index + 1])) {
      const headerCells = splitMarkdownTableRow(line);
      const alignments = parseMarkdownTableAlignments(lines[index + 1]);
      const bodyRows: string[][] = [];
      index += 2;
      while (index < lines.length && isMarkdownTableRow(lines[index])) {
        bodyRows.push(splitMarkdownTableRow(lines[index]));
        index += 1;
      }
      blocks.push(
        <div key={`block-${key++}`} className="markdown-table-wrap">
          <table className="markdown-table">
            <thead>
              <tr>
                {headerCells.map((cell, cellIndex) => (
                  <th key={`head-${cellIndex}`} style={tableCellStyle(alignments[cellIndex])}>
                    {renderInlineMarkdown(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bodyRows.map((row, rowIndex) => (
                <tr key={`row-${rowIndex}`}>
                  {headerCells.map((_, cellIndex) => (
                    <td key={`cell-${rowIndex}-${cellIndex}`} style={tableCellStyle(alignments[cellIndex])}>
                      {renderInlineMarkdown(row[cellIndex] ?? "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
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

type MarkdownTableAlignment = "left" | "center" | "right" | null;

function isMarkdownTableHeader(line: string, nextLine: string | undefined): boolean {
  if (!nextLine || !isMarkdownTableRow(line)) {
    return false;
  }
  const headerCells = splitMarkdownTableRow(line);
  const dividerCells = splitMarkdownTableRow(nextLine);
  if (headerCells.length < 2 || headerCells.length !== dividerCells.length) {
    return false;
  }
  return dividerCells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function isMarkdownTableRow(line: string): boolean {
  const trimmed = line.trim();
  if (!trimmed || !trimmed.includes("|")) {
    return false;
  }
  const cells = splitMarkdownTableRow(trimmed);
  return cells.length >= 2 && cells.some((cell) => cell.trim().length > 0);
}

function splitMarkdownTableRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let current = "";

  for (let index = 0; index < trimmed.length; index += 1) {
    const character = trimmed[index];
    if (character === "\\" && index + 1 < trimmed.length) {
      const nextCharacter = trimmed[index + 1];
      if (nextCharacter === "|" || nextCharacter === "\\") {
        current += nextCharacter;
        index += 1;
        continue;
      }
    }
    if (character === "|") {
      cells.push(current.trim());
      current = "";
      continue;
    }
    current += character;
  }

  cells.push(current.trim());
  return cells;
}

function parseMarkdownTableAlignments(line: string): MarkdownTableAlignment[] {
  return splitMarkdownTableRow(line).map((cell) => {
    const trimmed = cell.trim();
    if (trimmed.startsWith(":") && trimmed.endsWith(":")) {
      return "center";
    }
    if (trimmed.endsWith(":")) {
      return "right";
    }
    if (trimmed.startsWith(":")) {
      return "left";
    }
    return null;
  });
}

function tableCellStyle(alignment: MarkdownTableAlignment): CSSProperties | undefined {
  if (!alignment) {
    return undefined;
  }
  return { textAlign: alignment };
}

function upsertProject(projects: ProjectState[], nextProject: ProjectState): ProjectState[] {
  const nextProjectKey = projectPathKey(nextProject.path);
  const others = projects.filter((project) => projectPathKey(project.path) !== nextProjectKey);
  return [...others, nextProject].sort((left, right) => left.label.localeCompare(right.label));
}

function projectPathKey(path: string | null | undefined): string {
  let normalized = String(path ?? "")
    .trim()
    .replace(/^\\\\\?\\/, "")
    .replace(/\\/g, "/")
    .replace(/\/+$/, "");
  if (/^[a-zA-Z]:\//.test(normalized) || normalized.startsWith("//")) {
    normalized = normalized.toLowerCase();
  }
  return normalized;
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
  const { t } = useI18n();
  if (!summary || summary.openItems.length === 0) {
    return null;
  }

  const focusItem = summary.activeItem ?? summary.nextItem ?? summary.openItems[0] ?? null;
  const focusStatus = normalizeTodoStatus(focusItem?.status);
  const focusPrefix = focusStatus === "in_progress" ? t("todo.inProgress") : t("todo.next");
  const focusLabel = focusItem ? formatTodoLabel(focusItem) : "";

  return (
    <section className={`todo-status-bar ${expanded ? "expanded" : ""}`} aria-label={t("todo.progressLabel")}>
      <div className="todo-status-main">
        <div className="todo-status-pulse" aria-hidden="true" />
        <div className="todo-status-copy">
          <div className="todo-status-line">
            <strong>{t("todo.title")}</strong>
            <span>
              {t("todo.progress", { completed: summary.completedCount, total: summary.visibleItems.length })}
            </span>
          </div>
          {focusLabel ? (
            <p>
              <span>{focusPrefix}:</span> {focusLabel}
            </p>
          ) : null}
        </div>
        <button className="todo-toggle" type="button" onClick={onToggleExpanded}>
          {expanded ? t("todo.hide") : t("todo.showAll")}
        </button>
      </div>
      {expanded ? (
        <div className="todo-status-list">
          {summary.visibleItems.map((item, index) => {
            const status = normalizeTodoStatus(item.status);
            const label = formatTodoLabel(item);
            return (
              <div key={`${status}-${index}-${label}`} className={`todo-status-item ${status}`}>
                <span aria-hidden="true">{todoStatusMarker(status)}</span>
                <p>{label || t("todo.untitled")}</p>
              </div>
            );
          })}
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

function visibleSessionsForProject(
  projectPath: string,
  sessions: AgentSession[],
  archivedSessions: ArchivedSessionsState,
): AgentSession[] {
  const archivedIds = new Set(archivedSessions[projectPath] ?? []);
  return sessions.filter((session) => !archivedIds.has(session.id));
}

function appendArchivedSession(
  archivedSessions: ArchivedSessionsState,
  projectPath: string,
  sessionId: string,
): ArchivedSessionsState {
  const current = archivedSessions[projectPath] ?? [];
  if (current.includes(sessionId)) {
    return archivedSessions;
  }
  return {
    ...archivedSessions,
    [projectPath]: [...current, sessionId],
  };
}

function restoreArchivedSessions(
  archivedSessions: ArchivedSessionsState,
  entries: ArchivedSessionEntry[],
): ArchivedSessionsState {
  const grouped = new Map<string, Set<string>>();
  for (const entry of entries) {
    const current = grouped.get(entry.projectPath) ?? new Set<string>();
    current.add(entry.session.id);
    grouped.set(entry.projectPath, current);
  }
  const next: ArchivedSessionsState = { ...archivedSessions };
  for (const [projectPath, sessionIds] of grouped.entries()) {
    const remaining = (next[projectPath] ?? []).filter((sessionId) => !sessionIds.has(sessionId));
    if (remaining.length > 0) {
      next[projectPath] = remaining;
    } else {
      delete next[projectPath];
    }
  }
  return next;
}

function buildArchivedSessionEntries(
  projects: ProjectState[],
  archivedSessions: ArchivedSessionsState,
): ArchivedSessionEntry[] {
  const entries: ArchivedSessionEntry[] = [];
  for (const project of projects) {
    const archivedIds = new Set(archivedSessions[project.path] ?? []);
    for (const session of project.sessions) {
      if (!archivedIds.has(session.id)) {
        continue;
      }
      entries.push({
        key: `${project.path}::${session.id}`,
        projectPath: project.path,
        projectLabel: project.label,
        session,
        preview: buildSessionPreview(session),
        updatedAt: session.updated_at ?? session.created_at ?? null,
      });
    }
  }
  entries.sort((left, right) => (right.updatedAt ?? 0) - (left.updatedAt ?? 0));
  return entries;
}

function readStoredArchivedSessions(): ArchivedSessionsState {
  if (typeof window === "undefined") {
    return {};
  }
  try {
    const value = JSON.parse(window.localStorage.getItem(ARCHIVED_SESSIONS_STORAGE_KEY) ?? "{}") as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return {};
    }
    const entries = Object.entries(value as Record<string, unknown>);
    return Object.fromEntries(
      entries.map(([projectPath, sessionIds]) => [
        projectPath,
        Array.isArray(sessionIds)
          ? sessionIds.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
          : [],
      ]),
    );
  } catch {
    return {};
  }
}

function persistArchivedSessions(archivedSessions: ArchivedSessionsState) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(ARCHIVED_SESSIONS_STORAGE_KEY, JSON.stringify(archivedSessions));
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
  const projectKey = projectPathKey(projectPath);
  if (!paths.some((path) => projectPathKey(path) === projectKey)) {
    window.localStorage.setItem(PROJECTS_STORAGE_KEY, JSON.stringify([...paths, projectPath]));
  }
}

function removeStoredProjectPath(projectPath: string) {
  if (typeof window === "undefined") {
    return;
  }
  const projectKey = projectPathKey(projectPath);
  const paths = readStoredProjectPaths().filter((path) => projectPathKey(path) !== projectKey);
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

function uiCommandTarget(command: string): UiCommandTarget | null {
  const normalized = command.trim().split(/\s+/, 1)[0].toLowerCase();
  if (normalized === "/providers") {
    return { kind: "config", target: "provider" };
  }
  if (normalized === "/mcp") {
    return { kind: "config", target: "mcp" };
  }
  if (normalized === "/hooks") {
    return { kind: "config", target: "hooks" };
  }
  if (normalized === "/skills") {
    return { kind: "config", target: "skills" };
  }
  if (normalized === "/model" || normalized === "/reasoning") {
    return { kind: "model" };
  }
  if (normalized === "/compact") {
    return { kind: "context", command: "compact" };
  }
  if (normalized === "/janitor") {
    return { kind: "context", command: "janitor" };
  }
  return null;
}

function pendingUiCommandTarget(value: string, images: PendingImage[]): UiCommandTarget | null {
  if (images.length > 0) {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed.startsWith("/")) {
    return null;
  }
  return uiCommandTarget(trimmed);
}

function scrollSettingsPanelIntoView(panelName: string) {
  const panel = document.querySelector<HTMLElement>(`[data-settings-panel="${panelName}"]`);
  const scroller = panel?.closest<HTMLElement>(".settings-group");
  if (!panel || !scroller) {
    return;
  }
  scroller.scrollTo({
    top: Math.max(panel.offsetTop - scroller.offsetTop - 12, 0),
    behavior: "smooth",
  });
}

function currentPathMention(value: string, cursor: number): { query: string; queryStart: number; end: number } | null {
  const safeCursor = Math.max(0, Math.min(cursor, value.length));
  const beforeCursor = value.slice(0, safeCursor);
  const match = PATH_MENTION_PATTERN.exec(beforeCursor);
  if (!match) {
    return null;
  }
  const query = match[2] ?? "";
  return {
    query,
    queryStart: safeCursor - query.length,
    end: safeCursor,
  };
}

async function buildPromptPayload(client: SidecarClient, prompt: string, images: PendingImage[]): Promise<PreparedPromptPayload> {
  if (images.length === 0) {
    return prompt;
  }
  const stagedImages = await Promise.all(images.map((image) => client.stageInlineImage(image)));
  const content: Array<Record<string, unknown>> = [];
  const text = prompt.trim() || "Look at this image.";
  if (text) {
    content.push({ type: "text", text });
  }
  for (const image of stagedImages) {
    content.push({
      type: "input_image",
      path: image.path,
      absolute_path: image.absolute_path,
      media_type: image.media_type,
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

function interactionTitle(interaction: InteractionRequestState, t: (key: import("./lib/i18n").TranslationKey, params?: Record<string, string | number>) => string): string {
  if (interaction.kind === "authorization") {
    const toolName = typeof interaction.payload.tool_name === "string" ? interaction.payload.tool_name : "tool";
    return t("decision.approveTool", { toolName });
  }
  const targetMode = typeof interaction.payload.target_mode === "string" ? interaction.payload.target_mode : "another mode";
  return t("decision.switchToMode", { targetMode });
}

function interactionSummary(interaction: InteractionRequestState, t: (key: import("./lib/i18n").TranslationKey, params?: Record<string, string | number>) => string): string {
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

function runtimeItemId(prefix: string, turnId: string | null | undefined): string {
  const turn = String(turnId ?? "turn").trim() || "turn";
  return `${turn}-${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function readEventString(value: unknown, fallback: string): string {
  const text = typeof value === "string" ? value.trim() : "";
  return text || fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function compactInlineText(text: string, limit: number): string {
  const compact = String(text ?? "").replace(/\s+/g, " ").trim();
  if (compact.length <= limit) {
    return compact;
  }
  return limit <= 3 ? compact.slice(0, limit) : `${compact.slice(0, limit - 3)}...`;
}

function formatElapsedSeconds(startedAt: number): string {
  const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return `${elapsed}s`;
}

function teamMemberSummary(member: TeamMemberActivity): string {
  if (typeof member.summary === "string" && member.summary.trim()) {
    return member.summary;
  }
  const pieces = [
    typeof member.role === "string" && member.role.trim() ? member.role.trim() : "",
    typeof member.activity === "string" && member.activity.trim() ? member.activity.trim().replace(/_/g, " ") : "",
    member.current_task_id !== null && member.current_task_id !== undefined ? `task #${member.current_task_id}` : "",
  ].filter(Boolean);
  return pieces.join(" | ") || "active";
}

function taskStatus(task: TaskGraphItem): string {
  return String(task.status ?? "pending").trim().toLowerCase() || "pending";
}

function taskStatusLabel(status: string): string {
  if (status === "in_progress") {
    return "in progress";
  }
  return status.replace(/_/g, " ");
}

function taskStatusCounts(tasks: TaskGraphItem[]): { total: number; pending: number; inProgress: number; completed: number } {
  return tasks.reduce(
    (counts, task) => {
      const status = taskStatus(task);
      counts.total += 1;
      if (status === "completed") {
        counts.completed += 1;
      } else if (status === "in_progress") {
        counts.inProgress += 1;
      } else {
        counts.pending += 1;
      }
      return counts;
    },
    { total: 0, pending: 0, inProgress: 0, completed: 0 },
  );
}

function buildTaskGraphLayout(tasks: TaskGraphItem[], options: { expanded?: boolean } = {}): TaskGraphLayout {
  const nodeWidth = options.expanded ? 250 : 190;
  const nodeHeight = options.expanded ? 104 : 86;
  const xGap = options.expanded ? 96 : 74;
  const yGap = options.expanded ? 30 : 22;
  const margin = 18;
  const taskById = new Map(tasks.map((task) => [Number(task.id), task]));
  const edges = buildTaskEdges(tasks, taskById);
  const incoming = new Map<number, number[]>();
  const outgoing = new Map<number, number[]>();
  for (const task of tasks) {
    incoming.set(Number(task.id), []);
    outgoing.set(Number(task.id), []);
  }
  for (const edge of edges) {
    incoming.get(edge.to)?.push(edge.from);
    outgoing.get(edge.from)?.push(edge.to);
  }
  const levels = new Map<number, number>();
  const visiting = new Set<number>();
  function resolveLevel(taskId: number): number {
    if (levels.has(taskId)) {
      return levels.get(taskId) ?? 0;
    }
    if (visiting.has(taskId)) {
      levels.set(taskId, 0);
      return 0;
    }
    visiting.add(taskId);
    const parents = incoming.get(taskId) ?? [];
    const level = parents.length ? Math.max(...parents.map((parentId) => resolveLevel(parentId) + 1)) : 0;
    visiting.delete(taskId);
    levels.set(taskId, level);
    return level;
  }
  for (const task of tasks) {
    resolveLevel(Number(task.id));
  }
  const layers = new Map<number, TaskGraphItem[]>();
  for (const task of tasks) {
    const level = levels.get(Number(task.id)) ?? 0;
    layers.set(level, [...(layers.get(level) ?? []), task]);
  }
  const maxLayerSize = Math.max(1, ...Array.from(layers.values()).map((items) => items.length));
  const maxLevel = Math.max(0, ...Array.from(layers.keys()));
  const nodes: TaskGraphNodeLayout[] = [];
  for (const [level, layerTasks] of layers.entries()) {
    const sortedLayer = [...layerTasks].sort((left, right) => Number(left.id) - Number(right.id));
    const layerHeight = sortedLayer.length * nodeHeight + Math.max(0, sortedLayer.length - 1) * yGap;
    const graphHeight = maxLayerSize * nodeHeight + Math.max(0, maxLayerSize - 1) * yGap;
    const offsetY = (graphHeight - layerHeight) / 2;
    sortedLayer.forEach((task, index) => {
      nodes.push({
        task,
        level,
        x: margin + level * (nodeWidth + xGap),
        y: margin + offsetY + index * (nodeHeight + yGap),
      });
    });
  }
  return {
    nodes,
    edges,
    width: margin * 2 + (maxLevel + 1) * nodeWidth + maxLevel * xGap,
    height: margin * 2 + maxLayerSize * nodeHeight + Math.max(0, maxLayerSize - 1) * yGap,
    nodeWidth,
    nodeHeight,
  };
}

function buildTaskEdges(tasks: TaskGraphItem[], taskById: Map<number, TaskGraphItem>): TaskGraphEdge[] {
  const seen = new Set<string>();
  const edges: TaskGraphEdge[] = [];
  function addEdge(from: number, to: number) {
    if (!taskById.has(from) || !taskById.has(to) || from === to) {
      return;
    }
    const key = `${from}->${to}`;
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    edges.push({ from, to });
  }
  for (const task of tasks) {
    const taskId = Number(task.id);
    for (const blocker of task.blockedBy ?? []) {
      addEdge(Number(blocker), taskId);
    }
    for (const blocked of task.blocks ?? []) {
      addEdge(taskId, Number(blocked));
    }
  }
  return edges;
}

function edgePath(edge: TaskGraphEdge, graph: TaskGraphLayout): string {
  const from = graph.nodes.find((node) => node.task.id === edge.from);
  const to = graph.nodes.find((node) => node.task.id === edge.to);
  if (!from || !to) {
    return "";
  }
  const nodeWidth = graph.nodeWidth;
  const nodeHeight = graph.nodeHeight;
  const startX = from.x + nodeWidth;
  const startY = from.y + nodeHeight / 2;
  const endX = to.x;
  const endY = to.y + nodeHeight / 2;
  const control = Math.max(36, (endX - startX) / 2);
  return `M ${startX} ${startY} C ${startX + control} ${startY}, ${endX - control} ${endY}, ${endX - 4} ${endY}`;
}

function svgLine(text: string, limit: number): string {
  return compactInlineText(text, limit);
}

function taskNodeMeta(task: TaskGraphItem, limit: number): string {
  const owner = task.owner ? `@${task.owner}` : task.preferred_owner ? `prefers ${task.preferred_owner}` : "unassigned";
  return compactInlineText(owner, limit);
}

function removeProjectActivityKeys<T>(state: Record<string, T>, projectPath: string): Record<string, T> {
  const prefix = `${projectPath}::`;
  let changed = false;
  const next: Record<string, T> = {};
  for (const [key, value] of Object.entries(state)) {
    if (key.startsWith(prefix)) {
      changed = true;
      continue;
    }
    next[key] = value;
  }
  return changed ? next : state;
}

function findLastRunningToolIndex(items: ConversationRuntimeItem[], toolName: string): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.type === "tool_call" && item.toolCall.name === toolName && item.toolCall.status === "running") {
      return index;
    }
  }
  return -1;
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
