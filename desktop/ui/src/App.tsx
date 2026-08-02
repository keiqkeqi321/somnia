import {
  memo,
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
  type WheelEvent as ReactWheelEvent,
} from "react";
import mermaid from "mermaid";
import { VList, type VListHandle } from "virtua";
import appIconUrl from "../src-tauri/icons/32x32.png";

import {
  chooseProjectFolder,
  ensureManagedSidecar,
  hideMainWindow,
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
  normalizeToolContentBlocks,
  sortSessions,
  stringifyToolValue,
} from "./lib/messages";
import SettingsView, { ProviderProfilesEditor, type ArchivedSessionEntry, type SettingsSectionKey } from "./components/SettingsView";
import { useI18n, type TranslationKey } from "./lib/i18n";
import { useIsMobile } from "./lib/use-media-query";
import { normalizeBaseUrl, SidecarClient } from "./lib/sidecar";
import {
  createConversationState,
  readSessionPayload,
  transitionConversationEvent,
  type ConversationState,
} from "./lib/conversation-state";
import { DirectSomniaClient, type SomniaClient } from "./lib/somnia-client";
import { RemoteSomniaConnection } from "./lib/remote-somnia-connection";
import {
  isRemoteProjectPath,
  readRemoteLastTarget,
  remoteProjectPath,
  remoteScopedStorageKey,
  writeRemoteLastTarget,
} from "./lib/remote-storage";
import { navigateRemoteRoute, parseRemoteRoute, resolveRemoteRoute, useRemoteRouteHash } from "./lib/remote-router";
import { useWorkspaceImageSource } from "./lib/workspace-image";
import { useRemoteAccess } from "./lib/use-remote-access";
import RemoteConnectPage from "./components/RemoteConnectPage";
import RemoteLoginPage from "./components/RemoteLoginPage";
import RemotePairPage from "./components/RemotePairPage";
import RemoteRegisterPage from "./components/RemoteRegisterPage";
import type {
  AgentSession,
  ContextWindowUsage,
  ConversationContentBlock,
  ConversationPendingTurn,
  ConversationRuntimeItem,
  ConversationThinkingLog,
  ConversationToolCall,
  InteractionRequestState,
  ManagedSidecarConnection,
  McpServerSummary,
  ModelDescriptor,
  ProviderDescriptor,
  ProviderPresetDescriptor,
  RemoteProjectTarget,
  SettingsConfigScope,
  SettingsConfigScopeKey,
  SettingsConfigSectionKey,
  SidecarEvent,
  SidecarStatus,
  TaskGraphItem,
  TeamMemberActivity,
  TeamLogEntry,
  TeamLogDetail,
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
const LAST_OPENED_SESSION_STORAGE_KEY = "somnia.desktop.last-opened-session";
const DEFAULT_SIDECAR_URL = "http://127.0.0.1:8765";
const DEFAULT_REMOTE_RELAY_URL = "ws://127.0.0.1:8787";
const TOOL_LIMIT = 24;
const PROJECT_LIMIT = 5;
const SIDEBAR_MIN_WIDTH = 210;
const SIDEBAR_MAX_WIDTH = 430;
const CONTEXT_MIN_WIDTH = 280;
const CONTEXT_MAX_WIDTH = 540;
const CONVERSATION_MIN_WIDTH = 430;
const RESIZER_WIDTH = 10;
const REASONING_LEVEL_OPTIONS = ["auto", "low", "medium", "high", "deep"] as const;
const CONVERSATION_BOTTOM_STICKY_THRESHOLD = 64;
let mermaidRenderCounter = 0;
const MERMAID_MIN_ZOOM = 0.25;
const MERMAID_MAX_ZOOM = 4;
const MERMAID_ZOOM_STEP = 0.2;
const TOOL_IMAGE_MIN_SCALE = 0.75;
const TOOL_IMAGE_MAX_SCALE = 4;
const TOOL_IMAGE_SCALE_STEP = 0.15;

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "dark",
  fontFamily: '"Segoe UI Variable", "Aptos", "IBM Plex Sans", sans-serif',
});

const COMMAND_SPECS = [
  { command: "/init", descriptionKey: "cmd.init" as const },
  { command: "/scan", descriptionKey: "cmd.scan" as const },
  { command: "/symbols", descriptionKey: "cmd.symbols" as const },
  { command: "/image", descriptionKey: "cmd.image" as const },
  { command: "/paste-image", descriptionKey: "cmd.pasteImage" as const },
  { command: "/model", descriptionKey: "cmd.model" as const },
  { command: "/vision", descriptionKey: "cmd.vision" as const },
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
type LastOpenedSessionState = {
  projectPath: string;
  sessionId: string;
};
type ActiveProjectTurn = {
  sessionId: string;
  turnId: string | null;
  baseMessageCount: number;
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
type ToolImagePreviewState = {
  src: string;
  label: string;
};
type SelectedWorkerView = {
  conversationKey: string;
  name: string;
  sessionId: string | null;
};
type WorkerLogState = {
  loading: boolean;
  error: string | null;
  log: TeamLogDetail | null;
};

const DEFAULT_CONVERSATION_PROJECT_KEY = "__default_project__";
const SUBAGENT_FACTS_LIMIT = 5;

function App({ remoteMode = false }: { remoteMode?: boolean }) {
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
  const [visionModels, setVisionModels] = useState<ModelDescriptor[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [selectedVisionProvider, setSelectedVisionProvider] = useState("");
  const [selectedVisionModel, setSelectedVisionModel] = useState("");
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
  const [projectLimitNotice, setProjectLimitNotice] = useState<string | null>(null);
  const [projectMenuOpenKey, setProjectMenuOpenKey] = useState<string | null>(null);
  const [sessionMenuOpenKey, setSessionMenuOpenKey] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSectionKey>("provider");
  const [settingsConfigScopes, setSettingsConfigScopes] = useState<SettingsConfigScope[]>([]);
  const [settingsConfigDrafts, setSettingsConfigDrafts] = useState<Record<string, string>>({});
  const [settingsMcpServers, setSettingsMcpServers] = useState<McpServerSummary[]>([]);
  const [settingsConfigScope, setSettingsConfigScope] = useState<SettingsConfigScopeKey>("project");
  const [settingsConfigSection, setSettingsConfigSection] = useState<SettingsConfigSectionKey>("provider");
  const [settingsConfigLoading, setSettingsConfigLoading] = useState(false);
  const [settingsConfigSaving, setSettingsConfigSaving] = useState(false);
  const [settingsConfigMessage, setSettingsConfigMessage] = useState("");
  const [providerSetupOpen, setProviderSetupOpen] = useState(false);
  const [providerSetupScope, setProviderSetupScope] = useState<SettingsConfigScopeKey>("user");
  const [providerSetupDraft, setProviderSetupDraft] = useState("");
  const [providerSetupMode, setProviderSetupMode] = useState<"preset" | "custom">("preset");
  const [providerSetupPresets, setProviderSetupPresets] = useState<ProviderPresetDescriptor[]>([]);
  const [providerSetupSelectedPreset, setProviderSetupSelectedPreset] = useState("");
  const [providerSetupProviderName, setProviderSetupProviderName] = useState("");
  const [providerSetupProviderType, setProviderSetupProviderType] = useState("openai");
  const [providerSetupBaseUrl, setProviderSetupBaseUrl] = useState("");
  const [providerSetupApiKey, setProviderSetupApiKey] = useState("");
  const [providerSetupModelsText, setProviderSetupModelsText] = useState("");
  const [providerSetupDefaultModel, setProviderSetupDefaultModel] = useState("");
  const [providerSetupLoading, setProviderSetupLoading] = useState(false);
  const [providerSetupSaving, setProviderSetupSaving] = useState(false);
  const [providerSetupMessage, setProviderSetupMessage] = useState("");
  const [windowMaximized, setWindowMaximized] = useState(false);
  const [contextPanelOpen, setContextPanelOpen] = useState(false);
  const [sidebarDrawerOpen, setSidebarDrawerOpen] = useState(false);
  const isMobile = useIsMobile();
  const [todoExpanded, setTodoExpanded] = useState(false);
  const [layout, setLayout] = useState<LayoutState>(() => readStoredLayout());
  const [layoutDragging, setLayoutDragging] = useState<LayoutDragState | null>(null);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modePickerOpen, setModePickerOpen] = useState(false);
  const [contextPopoverOpen, setContextPopoverOpen] = useState(false);
  const [taskGraphPanelOpen, setTaskGraphPanelOpen] = useState(false);
  const [toolImagePreview, setToolImagePreview] = useState<ToolImagePreviewState | null>(null);
  const [selectedWorkerView, setSelectedWorkerView] = useState<SelectedWorkerView | null>(null);
  const [workerLogState, setWorkerLogState] = useState<WorkerLogState>({ loading: false, error: null, log: null });
  const [archivedSessions, setArchivedSessions] = useState<ArchivedSessionsState>(() => readStoredArchivedSessions());
  const [selectedArchivedSessionKeys, setSelectedArchivedSessionKeys] = useState<string[]>([]);
  const [bannerMessage, setBannerMessage] = useState("Point the UI at a running sidecar and start a session.");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [remoteConnected, setRemoteConnected] = useState(false);
  const [remoteConnecting, setRemoteConnecting] = useState(false);
  // Remote mode restores the cookie session (and the last connection) on
  // mount before any route is shown, so deep links into `#/workspace` survive
  // a refresh. Desktop mode never restores and never touches the hash router.
  const [remoteRestorePending, setRemoteRestorePending] = useState(remoteMode);
  const remoteRestoreAttemptedRef = useRef(false);
  const remoteAccess = useRemoteAccess(readRemoteRelayDefault());
  const remoteRouteHash = useRemoteRouteHash();
  const remoteRoute = resolveRemoteRoute({ authenticated: remoteAccess.authenticated, connected: remoteConnected, hash: remoteRouteHash });

  const clientRef = useRef<SomniaClient | null>(null);
  const projectClientsRef = useRef<Record<string, SomniaClient>>({});
  const conversationCoreStateRef = useRef<Record<string, ConversationState>>({});
  const selectedProjectPathRef = useRef<string | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const currentSessionRef = useRef<AgentSession | null>(null);
  const queuedPromptsRef = useRef<Record<string, QueuedPrompt[]>>({});
  const pendingAssistantDeltasRef = useRef<Record<string, { turnId: string | null | undefined; text: string }>>({});
  const assistantDeltaFlushTimerRef = useRef<number | null>(null);
  const activeProjectTurnsRef = useRef<Record<string, ActiveProjectTurn[]>>({});
  const workspaceRef = useRef<HTMLElement | null>(null);
  const modelPickerRef = useRef<HTMLDivElement | null>(null);
  const modePickerRef = useRef<HTMLDivElement | null>(null);
  const contextPopoverRef = useRef<HTMLDivElement | null>(null);
  const projectMenuRef = useRef<HTMLDivElement | null>(null);
  const sessionMenuRef = useRef<HTMLDivElement | null>(null);
  const composerTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const conversationListRef = useRef<VListHandle | null>(null);
  const conversationVirtualCountRef = useRef(0);
  const conversationPinnedToBottomRef = useRef(true);
  const conversationScrollFrameRef = useRef<number | null>(null);

  selectedSessionIdRef.current = selectedSessionId;
  selectedProjectPathRef.current = selectedProjectPath;
  currentSessionRef.current = currentSession;
  queuedPromptsRef.current = queuedPrompts;
  activeProjectTurnsRef.current = activeProjectTurns;

  function scrollConversationBodyToBottom() {
    if (conversationScrollFrameRef.current !== null) {
      window.cancelAnimationFrame(conversationScrollFrameRef.current);
    }
    conversationScrollFrameRef.current = window.requestAnimationFrame(() => {
      conversationScrollFrameRef.current = null;
      const list = conversationListRef.current;
      const count = conversationVirtualCountRef.current;
      if (!list || count <= 0) {
        return;
      }
      list.scrollToIndex(count - 1, { align: "end" });
    });
  }

  useEffect(() => {
    if (!remoteMode) {
      void initializeConnection();
    }
    return () => {
      Object.values(projectClientsRef.current).forEach((client) => client.close());
    };
    // Intentionally run only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Remote mode: restore the relay cookie session once on mount, then try to
  // re-establish the last Device/Project connection so a `#/workspace` deep
  // link survives a refresh. Falls back to `#/connect` / `#/login` via the
  // route-sync effect below when restore is not possible.
  useEffect(() => {
    if (!remoteMode || remoteRestoreAttemptedRef.current) {
      return;
    }
    remoteRestoreAttemptedRef.current = true;
    void restoreRemoteSession().finally(() => setRemoteRestorePending(false));
    // Intentionally run only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Remote mode: keep the URL hash converged on the route matching the
  // current auth/connection state (empty or hand-edited hashes redirect too).
  useEffect(() => {
    if (!remoteMode || remoteRestorePending) {
      return;
    }
    if (parseRemoteRoute(remoteRouteHash) !== remoteRoute) {
      navigateRemoteRoute(remoteRoute, { replace: true });
    }
  }, [remoteMode, remoteRestorePending, remoteRoute, remoteRouteHash]);

  useEffect(() => {
    const providerMissing = connectionState === "connected" && Boolean(status) && (status?.provider === "unconfigured" || providers.length === 0);
    if (providerMissing) {
      void openProviderSetupModal();
    } else if (providerSetupOpen && !providerSetupSaving) {
      setProviderSetupOpen(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionState, status?.provider, providers.length]);

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

    // Seed the picker with the current session's effective model so the
    // highlighted option matches what the trigger already shows. A pinned
    // session can differ from the workspace default; without this the picker
    // would open on the workspace default instead of the session's pin.
    const pinnedSession = currentSessionRef.current;
    if (pinnedSession?.provider_override && pinnedSession?.model_override) {
      setSelectedProvider(pinnedSession.provider_override);
      setSelectedModel(pinnedSession.model_override);
      void refreshModels(pinnedSession.provider_override, clientRef.current ?? undefined, pinnedSession.model_override);
    }

    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [modelPickerOpen]);

  useEffect(() => {
    if (!toolImagePreview) {
      return;
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setToolImagePreview(null);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toolImagePreview]);

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

  useEffect(() => {
    if (projects.length < PROJECT_LIMIT && projectLimitNotice) {
      setProjectLimitNotice(null);
    }
  }, [projectLimitNotice, projects.length]);

  // Managed project sidecars come and go (async startup, restarts with fresh
  // ephemeral ports). While remote control is enabled, push the live project
  // set to the hosting sidecar so its Connector re-registers Projects in
  // place — no manual disable/enable cycle, no Relay reconnect.
  const managedSidecarSignature = projects
    .map((project) => project.connection?.baseUrl ?? "")
    .filter((baseUrl) => baseUrl !== "")
    .sort()
    .join("|");
  const remoteReapplyInFlightRef = useRef(false);
  useEffect(() => {
    if (remoteMode || !managedSidecarSignature) {
      return;
    }
    const timer = window.setTimeout(() => {
      void (async () => {
        if (remoteReapplyInFlightRef.current) {
          return;
        }
        remoteReapplyInFlightRef.current = true;
        try {
          const managed = projects.filter((project) => project.connection !== null);
          let runningHost: SidecarClient | null = null;
          let enabledHost: SidecarClient | null = null;
          for (const project of managed) {
            const client = new SidecarClient(project.connection?.baseUrl ?? "");
            try {
              const remoteStatus = await client.getRemoteStatus();
              if (remoteStatus.connector_running) {
                runningHost = client;
                break;
              }
              if (remoteStatus.enabled && enabledHost === null) {
                enabledHost = client;
              }
            } catch {
              // An unreachable sidecar simply cannot host the Connector.
            }
          }
          const host = runningHost ?? enabledHost;
          if (!host) {
            return;
          }
          const collected = await collectRemoteProjects();
          if (collected.length > 0) {
            await host.enableRemoteDevice(collected);
          }
        } catch {
          // A failed re-apply is retried on the next project-set change.
        } finally {
          remoteReapplyInFlightRef.current = false;
        }
      })();
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [managedSidecarSignature, remoteMode]);

  useLayoutEffect(() => {
    if (!selectedSessionId) {
      return;
    }
    conversationPinnedToBottomRef.current = true;
    scrollConversationBodyToBottom();
  }, [selectedSessionId]);

  useEffect(() => {
    return () => {
      if (conversationScrollFrameRef.current !== null) {
        window.cancelAnimationFrame(conversationScrollFrameRef.current);
      }
    };
  }, []);

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
        const lastOpenedSession = readLastOpenedSession();
        const lastOpenedProjectKey = projectPathKey(lastOpenedSession?.projectPath);
        const managedConnection = await ensureManagedSidecar();
        if (managedConnection) {
          const managedProjectKey = projectPathKey(managedConnection.workspaceRoot);
          await connectManagedProject(managedConnection, {
            selectProject: !lastOpenedProjectKey || managedProjectKey === lastOpenedProjectKey,
            expandProject: !lastOpenedProjectKey || managedProjectKey === lastOpenedProjectKey,
          });
          for (const projectPath of savedProjectPaths) {
            if (projectPathKey(projectPath) === projectPathKey(managedConnection.workspaceRoot)) {
              continue;
            }
            try {
              const projectConnection = await ensureManagedSidecar(projectPath);
              if (projectConnection) {
                await connectManagedProject(projectConnection, {
                  selectProject: projectPathKey(projectPath) === lastOpenedProjectKey,
                  expandProject: projectPathKey(projectPath) === lastOpenedProjectKey,
                });
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

  async function restoreRemoteSession() {
    const restored = await remoteAccess.restoreSession();
    if (!restored) {
      return;
    }
    // A `#/pair` deep link must not be overridden by the remembered
    // device/project auto-reconnect — the approval page is why the tab exists.
    if (parseRemoteRoute(window.location.hash) === "pair") {
      return;
    }
    const target = readRemoteLastTarget();
    if (!target) {
      return;
    }
    const device = remoteAccess.devicesRef.current.find((item) => item.device_id === target.deviceId && !item.revoked_at);
    const remoteProject = device?.projects.find((item) => item.project_id === target.projectId);
    if (!device || !remoteProject) {
      // The remembered target is gone (revoked Device or removed Project);
      // drop it and let the route-sync effect land on `#/connect`.
      writeRemoteLastTarget(null);
      return;
    }
    await connectRemoteProject(target.deviceId, target.projectId);
  }

  async function connectRemoteProject(deviceId: string, projectId: string) {
    const device = remoteAccess.devicesRef.current.find((item) => item.device_id === deviceId);
    const remoteProject = device?.projects.find((item) => item.project_id === projectId);
    if (!device || !remoteProject) {
      return;
    }
    if (!await remoteAccess.verifyAccess()) {
      return;
    }
    const projectPath = remoteProjectPath(deviceId, projectId);
    const client = new RemoteSomniaConnection({
      relayUrl: remoteAccess.relayUrl,
      deviceId,
      projectId,
      // On ws 4401 (the 15-minute access cookie expired), renew the cookie
      // before reconnecting so the connection does not fall into a loop.
      reauthorize: () => remoteAccess.verifyAccess(),
    });
    setRemoteConnecting(true);
    setConnectionState("connecting");
    setBannerMessage(`Connecting to ${device.name} / ${remoteProject.name}...`);
    try {
      openEventConnection(client, "", projectPath);
      await waitForConnectionOpen(client);
      const [runtimeStatus, sessionList, providerList, interactionList, logList] = await Promise.all([
        client.runtimeStatus(),
        client.listSessions(),
        client.listProviders(),
        client.listInteractions(),
        client.listToolLogs(TOOL_LIMIT),
      ]);
      const project: ProjectState = {
        path: projectPath,
        label: remoteProject.name,
        connection: null,
        status: runtimeStatus,
        connectionState: "connected",
        connectionError: null,
        sessions: sortSessions(sessionList),
        pendingInteractions: interactionList,
        toolLogs: logList,
      };
      setProjects((previous) => upsertProject(previous, project));
      setCollapsedProjects((previous) => ({
        ...previous,
        [projectPath]: false,
      }));
      writeRemoteLastTarget({ deviceId, projectId });
      // Enter the workspace only after the full initial load (providers,
      // models, session selection) settles, so the connecting screen covers
      // the whole startup window instead of flickering inside the workspace.
      await activateProject(projectPath, client, project);
      setRemoteConnected(true);
      setBannerMessage(`Connected to ${device.name} / ${remoteProject.name}.`);
    } catch (error) {
      delete projectClientsRef.current[projectPath];
      if (clientRef.current === client) {
        clientRef.current = null;
      }
      client.close();
      setConnectionState("error");
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setRemoteConnecting(false);
    }
  }

  function handleRemoteSwitchTarget() {
    Object.values(projectClientsRef.current).forEach((client) => client.close());
    projectClientsRef.current = {};
    clientRef.current = null;
    setProjects([]);
    setSelectedProjectPath(null);
    setStatus(null);
    setSessions([]);
    setSelectedSessionId(null);
    setCurrentSession(null);
    setPendingInteractions([]);
    setToolLogs([]);
    setActiveTurnId(null);
    setConnectionState("disconnected");
    setBannerMessage("Switch to another Device or Project.");
    writeRemoteLastTarget(null);
    setRemoteConnected(false);
  }

  function handleRemoteSignOut() {
    writeRemoteLastTarget(null);
    void remoteAccess.signOut();
  }

  async function collectRemoteProjects(): Promise<RemoteProjectTarget[]> {
    // The embedded Connector can only bridge loopback sidecars, so only
    // projects with a managed sidecar connection are exposed; each sidecar
    // reports its own desktop-<hash> project id (see /remote/project-id).
    const managed = projects.filter((project) => project.connection !== null);
    const collected = await Promise.all(
      managed.map(async (project) => {
        const connection = project.connection;
        if (!connection) {
          return null;
        }
        try {
          const projectId = await new SidecarClient(connection.baseUrl).getRemoteProjectId();
          return { project_id: projectId, name: project.label, base_url: connection.baseUrl };
        } catch {
          // A sidecar that does not answer cannot be exposed; skip it.
          return null;
        }
      }),
    );
    return collected.filter((entry): entry is RemoteProjectTarget => entry !== null);
  }

  async function connectManagedProject(
    managedConnection: ManagedSidecarConnection,
    options: { selectProject: boolean; expandProject?: boolean } = { selectProject: true },
  ) {
    const client = new DirectSomniaClient(managedConnection.baseUrl);
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

    setProjects((previous) => upsertProject(previous, project));
    persistProjectPath(projectPath);
    openEventConnection(client, runtimeStatus.ws_url, projectPath);
    setConnectionState("connected");

    if (options.expandProject) {
      setCollapsedProjects((previous) => ({
        ...previous,
        [projectPath]: false,
      }));
    }
    if (options.selectProject) {
      await activateProject(projectPath, client, project);
    } else if (!selectedProjectPathRef.current) {
      const lastOpenedSession = readLastOpenedSession(projectPath);
      if (!lastOpenedSession || projectPathKey(lastOpenedSession.projectPath) === projectPathKey(projectPath)) {
        await activateProject(projectPath, client, project);
      }
    }
  }

  async function activateProject(projectPath: string, client = projectClientsRef.current[projectPath], project?: ProjectState) {
    const nextProject = project ?? projects.find((item) => item.path === projectPath);
    if (!client || !nextProject || !nextProject.status) {
      return;
    }
    clientRef.current = client;
    setSelectedProjectPath(projectPath);
    setStatus(nextProject.status);
    setSessions(nextProject.sessions);
    setPendingInteractions(nextProject.pendingInteractions);
    setToolLogs(nextProject.toolLogs);
    setProviders(await client.listProviders());
    setSelectedProvider(nextProject.status.provider);
    setSelectedVisionProvider(nextProject.status.vision_provider ?? "");
    setSelectedVisionModel(nextProject.status.vision_model ?? "");
    setSelectedReasoningLevel(normalizeReasoningLevel(nextProject.status.reasoning_level));
    await refreshModels(nextProject.status.provider, client, nextProject.status.model);
    await refreshVisionModels(nextProject.status.vision_provider ?? "", client, nextProject.status.vision_model ?? "");

    const visibleSessions = visibleSessionsForProject(projectPath, nextProject.sessions, archivedSessions);
    if (isRemoteProjectPath(projectPath)) {
      // Remote projects keep prompt history in their own device/project bucket.
      setPromptHistory(readStoredPromptHistory(projectPath));
    }
    const lastOpenedSession = readLastOpenedSession(projectPath);
    const preferredSessionId =
      lastOpenedSession && projectPathKey(lastOpenedSession.projectPath) === projectPathKey(projectPath)
        ? lastOpenedSession.sessionId
        : selectedSessionIdRef.current;
    const nextSessionId = visibleSessions.find((session) => session.id === preferredSessionId)?.id ?? visibleSessions[0]?.id ?? null;
    if (nextSessionId) {
      await selectSession(nextSessionId, client, nextProject.sessions, projectPath);
    } else {
      setSelectedSessionId(null);
      setCurrentSession(null);
      clearLastOpenedSession(projectPath);
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
    clientRef.current?.close();

    try {
      const nextClient = new DirectSomniaClient(normalizedBaseUrl);
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
      setSelectedVisionProvider(runtimeStatus.vision_provider ?? "");
      setSelectedVisionModel(runtimeStatus.vision_model ?? "");
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
      openEventConnection(nextClient, runtimeStatus.ws_url, projectPath);
      setProjects((previous) => upsertProject(previous, project));
      setSelectedProjectPath(projectPath);
      setSessions(sortedSessions);
      setCollapsedProjects((previous) => ({
        ...previous,
        [projectPath]: false,
      }));
      const lastOpenedSession = readLastOpenedSession(projectPath);
      const preferredSessionId =
        lastOpenedSession && projectPathKey(lastOpenedSession.projectPath) === projectPathKey(projectPath)
          ? lastOpenedSession.sessionId
          : selectedSessionIdRef.current;
      const nextSessionId = visibleSessions.find((session) => session.id === preferredSessionId)?.id ?? visibleSessions[0]?.id ?? null;
      if (nextSessionId) {
        await selectSession(nextSessionId, nextClient, sortedSessions, projectPath);
      } else {
        setSelectedSessionId(null);
        setCurrentSession(null);
        clearLastOpenedSession(projectPath);
      }
      await refreshModels(runtimeStatus.provider, nextClient, runtimeStatus.model);
      await refreshVisionModels(runtimeStatus.vision_provider ?? "", nextClient, runtimeStatus.vision_model ?? "");
    } catch (error) {
      clientRef.current = null;
      setConnectionState("error");
      setBannerMessage(`${errorPrefix}${formatErrorMessage(error)}`);
      setStatus(null);
    }
  }

  function openEventConnection(client: SomniaClient, wsUrl: string, projectPath: string) {
    const previousClient = projectClientsRef.current[projectPath];
    if (previousClient && previousClient !== client) {
      previousClient.close();
    }
    if (client instanceof DirectSomniaClient) {
      client.setEventStreamUrl(wsUrl);
    }
    projectClientsRef.current[projectPath] = client;
    if (selectedProjectPathRef.current === projectPath || !selectedProjectPathRef.current) {
      clientRef.current = client;
    }
    client.subscribe((notification) => {
      if (notification.kind === "event") {
        void handleSidecarEvent(projectPath, notification.event);
        return;
      }
      if (notification.kind === "protocol_error") {
        setBannerMessage(notification.error);
        return;
      }
      if (notification.kind === "snapshot") {
        // Remote-only: the relay re-synced the full stream after a reconnect;
        // pull the authoritative state back into the UI.
        void resyncAfterRemoteSnapshot(projectPath, client);
        return;
      }
      setConnectionState(notification.state);
      const remoteProject = isRemoteProjectPath(projectPath);
      if (notification.state === "disconnected" && clientRef.current === client) {
        setBannerMessage(remoteProject ? t("remote.reconnecting") : "Sidecar event stream disconnected.");
      } else if (notification.state === "connecting" && remoteProject && clientRef.current === client) {
        setBannerMessage(t("remote.connectingDevice"));
      } else if (notification.state === "error") {
        setBannerMessage(notification.error ?? (remoteProject ? t("remote.connectionFailed") : "Sidecar event stream failed."));
      }
    });
  }

  async function resyncAfterRemoteSnapshot(projectPath: string, client: SomniaClient) {
    try {
      const sessionList = sortSessions(await client.listSessions());
      setProjects((previous) =>
        previous.map((project) =>
          projectPathKey(project.path) === projectPathKey(projectPath) ? { ...project, sessions: sessionList } : project,
        ),
      );
      if (clientRef.current !== client || selectedProjectPathRef.current !== projectPath) {
        return;
      }
      setSessions(sessionList);
      const statusResult = await refreshStatusAndProviders();
      restoreActiveTurnsFromStatus(projectPath, statusResult?.runtimeStatus.active_turns, sessionList);
      await refreshToolLogs();
      const sessionId = selectedSessionIdRef.current;
      if (sessionId && sessionList.some((session) => session.id === sessionId)) {
        setCurrentSession(await client.loadSession(sessionId));
      } else if (sessionId) {
        setSelectedSessionId(null);
        setCurrentSession(null);
        clearLastOpenedSession(projectPath, sessionId);
      }
      setBannerMessage(t("remote.resynced"));
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    }
  }

  // After a remote reconnect the server is the only authority on which turns
  // are still running; rebuild the project's in-flight turn markers so those
  // sessions show as "answering" again and keep accepting live stream events.
  function restoreActiveTurnsFromStatus(
    projectPath: string,
    activeTurns: SidecarStatus["active_turns"],
    sessionList: AgentSession[],
  ) {
    if (!Array.isArray(activeTurns)) {
      return;
    }
    const restored: ActiveProjectTurn[] = activeTurns
      .filter((turn) => turn && typeof turn.session_id === "string" && typeof turn.turn_id === "string")
      .map((turn) => ({
        sessionId: turn.session_id,
        turnId: turn.turn_id,
        baseMessageCount: sessionList.find((session) => session.id === turn.session_id)?.messages.length ?? 0,
      }));
    updateActiveProjectTurns((previous) => ({ ...previous, [projectPath]: restored.slice(-2) }));
    const selectedSessionId = selectedSessionIdRef.current;
    const selectedTurn = restored.find((turn) => turn.sessionId === selectedSessionId);
    if (selectedProjectPathRef.current === projectPath && selectedTurn) {
      setActiveTurnId(selectedTurn.turnId);
    }
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
    dropPendingAssistantDelta(key);
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
    dropPendingAssistantDelta(key);
    setRuntimeConversationItems((previous) => ({ ...previous, [key]: [] }));
  }

  function dropPendingAssistantDelta(key: string) {
    const pending = pendingAssistantDeltasRef.current;
    if (key in pending) {
      const next = { ...pending };
      delete next[key];
      pendingAssistantDeltasRef.current = next;
    }
  }

  function appendAssistantRuntimeDelta(projectPath: string | null | undefined, sessionId: string | null | undefined, turnId: string | null | undefined, delta: string) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key || !delta) {
      return;
    }
    // Buffer per-token deltas and flush them in batches so streaming does not
    // re-render the whole conversation for every single token.
    const pending = pendingAssistantDeltasRef.current;
    const existing = pending[key];
    pendingAssistantDeltasRef.current = {
      ...pending,
      [key]: { turnId, text: existing ? existing.text + delta : delta },
    };
    if (assistantDeltaFlushTimerRef.current === null) {
      assistantDeltaFlushTimerRef.current = window.setTimeout(() => {
        assistantDeltaFlushTimerRef.current = null;
        flushAssistantRuntimeDeltas();
      }, 50);
    }
  }

  function flushAssistantRuntimeDeltas() {
    if (assistantDeltaFlushTimerRef.current !== null) {
      window.clearTimeout(assistantDeltaFlushTimerRef.current);
      assistantDeltaFlushTimerRef.current = null;
    }
    const pending = pendingAssistantDeltasRef.current;
    const keys = Object.keys(pending);
    if (keys.length === 0) {
      return;
    }
    pendingAssistantDeltasRef.current = {};
    setRuntimeConversationItems((previous) => {
      let next = previous;
      for (const key of keys) {
        const entry = pending[key];
        const current = next[key] ?? [];
        const last = current[current.length - 1];
        if (last?.type === "assistant_text") {
          next = {
            ...next,
            [key]: [...current.slice(0, -1), { ...last, text: `${last.text}${entry.text}` }],
          };
        } else {
          next = {
            ...next,
            [key]: [
              ...current,
              {
                id: runtimeItemId("assistant", entry.turnId),
                type: "assistant_text",
                text: entry.text,
              },
            ],
          };
        }
      }
      return next;
    });
  }

  function upsertRuntimeThinkingLog(
    projectPath: string | null | undefined,
    sessionId: string | null | undefined,
    turnId: string | null | undefined,
    payload: Record<string, unknown>,
    status: "running" | "finished",
  ) {
    const key = conversationStateKey(projectPath, sessionId);
    if (!key) {
      return;
    }
    const itemId = `thinking-${String(turnId ?? payload.turn_id ?? "turn")}`;
    const delta = typeof payload.delta === "string" ? payload.delta : "";
    setRuntimeConversationItems((previous) => {
      const current = previous[key] ?? [];
      const matchIndex = current.findIndex((item) => item.type === "thinking_log" && item.id === itemId);
      const previousLog = matchIndex >= 0 && current[matchIndex].type === "thinking_log" ? current[matchIndex].thinkingLog : null;
      const characters = Number(payload.characters ?? previousLog?.characters ?? 0);
      const blockCount = Number(payload.block_count ?? previousLog?.blockCount ?? 0);
      const durationMs = Number(payload.duration_ms ?? previousLog?.durationMs ?? 0);
      const thinkingLog: ConversationThinkingLog = {
        turnId: typeof payload.turn_id === "string" ? payload.turn_id : turnId ?? previousLog?.turnId ?? null,
        path: typeof payload.path === "string" ? payload.path : previousLog?.path ?? null,
        text: status === "running" ? `${previousLog?.text ?? ""}${delta}` : previousLog?.text ?? "",
        characters: Number.isFinite(characters) ? Math.max(0, characters) : previousLog?.characters ?? 0,
        blockCount: Number.isFinite(blockCount) ? Math.max(0, blockCount) : previousLog?.blockCount ?? 0,
        durationMs: Number.isFinite(durationMs) ? Math.max(0, durationMs) : previousLog?.durationMs ?? null,
        status,
      };
      const item: ConversationRuntimeItem = {
        id: itemId,
        type: "thinking_log",
        thinkingLog,
        isStreaming: status === "running",
      };
      if (matchIndex < 0) {
        return { ...previous, [key]: [...current, item] };
      }
      return { ...previous, [key]: current.map((existing, index) => (index === matchIndex ? item : existing)) };
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
    const toolCallId = runtimeToolCallId(event);
    if (toolName === "subagent" && readEventString(event.payload.actor, "lead") === "lead") {
      noteSubagentStarted(projectPath, event);
    }
    setRuntimeConversationItems((previous) => ({
      ...previous,
      [key]: [
        ...(previous[key] ?? []),
        {
          id: runtimeItemId("tool", toolCallId),
          type: "tool_call",
          toolCall: {
            id: runtimeItemId("tool-call", toolCallId),
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
    const toolCallId = runtimeToolCallId(event);
    const finishedTool = {
      id: runtimeItemId("tool-call", toolCallId),
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
      const matchIndex = findLastRunningToolIndex(current, toolName, runtimeItemId("tool-call", toolCallId));
      if (matchIndex < 0) {
        return {
          ...previous,
          [key]: [...current, { id: runtimeItemId("tool", toolCallId), type: "tool_call", toolCall: finishedTool }],
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
    const coreKey = conversationStateKey(projectPath, event.session_id);
    let conversationTransition = null;
    let previousConversationState = null;
    if (coreKey && event.session_id) {
      const selectedSession = currentSessionRef.current;
      previousConversationState =
        conversationCoreStateRef.current[coreKey] ??
        createConversationState(selectedSession?.id === event.session_id ? selectedSession : event.session_id);
      conversationTransition = transitionConversationEvent(previousConversationState, event);
      conversationCoreStateRef.current[coreKey] = conversationTransition.state;
    }
    if (conversationTransition?.effect.type !== "assistant_delta") {
      flushAssistantRuntimeDeltas();
    }
    if (event.type === "sidecar_ready") {
      return;
    }
    if (event.type === "session_created") {
      const payloadSession = readSessionPayload(event.payload.session);
      if (payloadSession) {
        upsertProjectSession(projectPath, payloadSession);
        if (isActiveProject) {
          setSelectedSessionId(payloadSession.id);
          setCurrentSession(payloadSession);
          persistLastOpenedSession(projectPath, payloadSession.id);
        }
      }
      return;
    }
    if (conversationTransition?.effect.type === "turn_started") {
      const coreState = conversationTransition.state;
      if (coreState.sessionId) {
        updateActiveProjectTurns((previous) => ({
          ...previous,
          [projectPath]: (() => {
            const current = previous[projectPath] ?? [];
            const existing = current.find(
              (turn) => turn.turnId === coreState.activeTurnId || turn.sessionId === coreState.sessionId,
            );
            const selectedSession = currentSessionRef.current;
            const baseMessageCount =
              existing?.baseMessageCount ??
              (selectedSession && coreState.sessionId === selectedSession.id ? selectedSession.messages.length : 0);
            return [
              ...current.filter(
                (turn) => turn.turnId !== coreState.activeTurnId && turn.sessionId !== coreState.sessionId,
              ),
              {
                sessionId: coreState.sessionId,
                turnId: coreState.activeTurnId,
                baseMessageCount,
              },
            ].slice(-2);
          })(),
        }));
      }
      if (isActiveProject && coreState.sessionId === selectedSessionIdRef.current) {
        setActiveTurnId(coreState.activeTurnId);
      }
      return;
    }
    if (conversationTransition?.effect.type === "assistant_delta") {
      const coreState = conversationTransition.state;
      const delta = coreState.assistantText.slice(previousConversationState?.assistantText.length ?? 0);
      appendAssistantRuntimeDelta(projectPath, coreState.sessionId, coreState.activeTurnId, delta);
      return;
    }
    if (event.type === "thinking_delta") {
      upsertRuntimeThinkingLog(projectPath, event.session_id, event.turn_id, event.payload, "running");
      return;
    }
    if (event.type === "thinking_finished") {
      upsertRuntimeThinkingLog(projectPath, event.session_id, event.turn_id, event.payload, "finished");
      return;
    }
    if (event.type === "context_usage_updated") {
      const sessionId = event.session_id ?? "";
      const contextWindowUsage = readContextUsageFromPayload(event.payload.context_window_usage);
      if (!sessionId || !contextWindowUsage) {
        return;
      }
      setSessions((previous) =>
        previous.map((session) => (session.id === sessionId ? { ...session, context_window_usage: contextWindowUsage } : session)),
      );
      setProjects((previous) =>
        previous.map((project) =>
          project.path === projectPath
            ? {
                ...project,
                sessions: project.sessions.map((session) =>
                  session.id === sessionId ? { ...session, context_window_usage: contextWindowUsage } : session,
                ),
              }
            : project,
        ),
      );
      if (isActiveProject && sessionId === selectedSessionIdRef.current) {
        setCurrentSession((session) => (session && session.id === sessionId ? { ...session, context_window_usage: contextWindowUsage } : session));
      }
      return;
    }
    if (event.type === "session_updated") {
      const payloadSession = readSessionPayload(event.payload.session);
      if (payloadSession) {
        const sessionHasActiveTurn = (activeProjectTurnsRef.current[projectPath] ?? []).some((turn) => turn.sessionId === payloadSession.id);
        upsertProjectSession(projectPath, payloadSession);
        if (!sessionHasActiveTurn && isActiveProject && payloadSession.id === selectedSessionIdRef.current) {
          setCurrentSession(payloadSession);
        }
        if (!sessionHasActiveTurn) {
          clearConversationRuntimeState(projectPath, payloadSession.id);
        }
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
      if (toolName === "task_create_batch" || toolName === "task_update" || toolName === "task_list" || toolName === "claim_task") {
        void refreshTaskGraph(projectPath, event.session_id ?? null);
      }
      return;
    }
    if (event.type === "subagent_activity") {
      noteSubagentActivity(projectPath, event);
      return;
    }
    if (
      event.type === "provider_switched" ||
      event.type === "vision_model_updated" ||
      event.type === "reasoning_level_updated" ||
      event.type === "execution_mode_updated"
    ) {
      if (isActiveProject) {
        void refreshStatusAndProviders();
      }
      return;
    }
    if (event.type === "session_model_updated") {
      // A session's per-session model pin changed. Refresh the loaded session
      // so the trigger reflects the new effective model; this is the event the
      // model picker's "Apply" produces, so we don't need a full status refresh.
      if (isActiveProject && event.session_id && event.session_id === selectedSessionIdRef.current) {
        const payloadSession = event.payload?.session as AgentSession | undefined;
        if (payloadSession) {
          setCurrentSession(payloadSession);
        } else if (clientRef.current) {
          try {
            setCurrentSession(await clientRef.current.loadSession(event.session_id));
          } catch {
            // Best-effort refresh; the trigger falls back to the workspace default.
          }
        }
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
    if (conversationTransition?.effect.type === "turn_completed") {
      const coreState = conversationTransition.state;
      const completedTurnId = previousConversationState?.activeTurnId ?? event.turn_id ?? null;
      clearActiveProjectTurn(projectPath, completedTurnId);
      clearConversationRuntimeState(projectPath, coreState.sessionId);
      clearActivityState(projectPath, coreState.sessionId);
      const completedSessionId = coreState.sessionId;
      if (isActiveProject) {
        if (completedSessionId === selectedSessionIdRef.current) {
          setActiveTurnId((current) => (current === completedTurnId ? null : current));
        }
      }
      const payloadSession = coreState.session;
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

  async function refreshStatusAndProviders(): Promise<{ runtimeStatus: SidecarStatus; providerList: ProviderDescriptor[] } | null> {
    const client = clientRef.current;
    if (!client) {
      return null;
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
    setSelectedVisionProvider(runtimeStatus.vision_provider ?? "");
    setSelectedVisionModel(runtimeStatus.vision_model ?? "");
    setSelectedReasoningLevel(normalizeReasoningLevel(runtimeStatus.reasoning_level));
    updateActiveProject({ status: runtimeStatus, pendingInteractions: interactionList });
    await refreshModels(runtimeStatus.provider, client, runtimeStatus.model);
    await refreshVisionModels(runtimeStatus.vision_provider ?? "", client, runtimeStatus.vision_model ?? "");
    return { runtimeStatus, providerList };
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

  async function refreshVisionModels(providerName: string, client = clientRef.current, preferredModel?: string) {
    if (!client || !providerName) {
      setVisionModels([]);
      setSelectedVisionModel("");
      return;
    }
    const nextModels = await client.listModels(providerName);
    setVisionModels(nextModels);
    setSelectedVisionModel(preferredModel ?? nextModels.find((model) => model.is_vision)?.name ?? nextModels[0]?.name ?? "");
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
      if (projectPath) {
        persistLastOpenedSession(projectPath, created.id);
      }
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
    const connection = projectPath ? projectClientsRef.current[projectPath] : clientRef.current;
    if (!connection) {
      throw new Error("Somnia connection is unavailable.");
    }
    const loadedSession = await connection.query({ type: "session.load", sessionId });
    const coreKey = conversationStateKey(projectPath, sessionId);
    if (coreKey) {
      conversationCoreStateRef.current[coreKey] = createConversationState(loadedSession);
    }
    setSelectedSessionId(sessionId);
    setCurrentSession(loadedSession);
    if (projectPath) {
      persistLastOpenedSession(projectPath, sessionId);
    }
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
    clearLastOpenedSession(projectPath, sessionId);
  }

  function updateActiveProject(patch: Partial<Pick<ProjectState, "status" | "pendingInteractions" | "toolLogs" | "sessions">>) {
    const projectPath = selectedProjectPathRef.current;
    if (!projectPath) {
      return;
    }
    setProjects((previous) => previous.map((project) => (project.path === projectPath ? { ...project, ...patch } : project)));
  }

  function updateActiveProjectTurns(updater: (previous: Record<string, ActiveProjectTurn[]>) => Record<string, ActiveProjectTurn[]>) {
    setActiveProjectTurns((previous) => {
      const next = updater(previous);
      activeProjectTurnsRef.current = next;
      return next;
    });
  }

  function clearActiveProjectTurn(projectPath: string, turnId: string | null) {
    updateActiveProjectTurns((previous) => {
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
    if (remoteMode) {
      return;
    }
    if (projects.length >= PROJECT_LIMIT) {
      const message = t("sidebar.projectLimitReached", { count: PROJECT_LIMIT });
      setProjectLimitNotice(message);
      setBannerMessage(message);
      return;
    }
    setProjectLimitNotice(null);
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
      await connectManagedProject(managedConnection, { selectProject: true, expandProject: true });
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
      if (projectPath) {
        persistLastOpenedSession(projectPath, session.id);
      }
      setSidebarSection("sessions");
      setDraft("");
      clearConversationRuntimeState(projectPath, session.id);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleRemoveProject(projectPath: string) {
    if (remoteMode) {
      return;
    }
    const project = projects.find((item) => item.path === projectPath);
    if (!project) {
      return;
    }
    setBusyAction("remove-project");
    try {
      projectClientsRef.current[projectPath]?.close();
      delete projectClientsRef.current[projectPath];
      await stopManagedSidecar(projectPath);
      removeStoredProjectPath(projectPath);
      clearLastOpenedSession(projectPath);
      updateActiveProjectTurns((previous) => {
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
    client: SomniaClient,
    projectPath: string | null,
    session: AgentSession,
    prompt: string,
    images: PendingImage[],
  ) {
    const optimisticTurnId = `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const key = conversationStateKey(projectPath, session.id);
    const optimisticUserText = buildOptimisticUserText(prompt, images);
    const baseMessageCount = session.messages.length;
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
    if (projectPath) {
      updateActiveProjectTurns((previous) => ({
        ...previous,
        [projectPath]: [
          ...(previous[projectPath] ?? []).filter((turn) => turn.sessionId !== session.id),
          { sessionId: session.id, turnId: optimisticTurnId, baseMessageCount },
        ].slice(-2),
      }));
    }
    const userInput = await buildPromptPayload(client, prompt, images);
    const connection = projectPath ? projectClientsRef.current[projectPath] : clientRef.current;
    if (!connection) {
      throw new Error("Somnia connection is unavailable.");
    }
    const response = await connection.execute({ type: "turn.start", sessionId: session.id, userInput });
    if (key) {
      setPendingTurns((previous) => {
        const current = previous[key];
        if (!current || current.id !== optimisticTurnId) {
          return previous;
        }
        return { ...previous, [key]: { ...current, id: response.turn_id, sessionId: session.id } };
      });
    }
    if (projectPath) {
      updateActiveProjectTurns((previous) => ({
        ...previous,
        [projectPath]: (previous[projectPath] ?? []).map((turn) =>
          turn.turnId === optimisticTurnId ? { ...turn, turnId: response.turn_id } : turn,
        ),
      }));
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

  async function handleVisionProviderChange(nextProvider: string) {
    setSelectedVisionProvider(nextProvider);
    try {
      await refreshVisionModels(nextProvider);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    }
  }

  async function handleApplyProviderModel() {
    const client = clientRef.current;
    if (!client || !selectedProvider || !selectedModel) {
      return;
    }
    const session = currentSessionRef.current;
    setBusyAction("switch-provider");
    try {
      if (session) {
        // Pin this session to the chosen model instead of flipping the
        // workspace-wide default, so other sessions (including any turn that
        // is mid-flight) keep running on their own model.
        const result = await client.setSessionModel(session.id, selectedProvider, selectedModel);
        setCurrentSession(result.session);
        setBannerMessage(result.message);
      } else {
        // No session selected: fall back to changing the workspace default.
        await client.switchProviderModel(selectedProvider, selectedModel);
        await client.setReasoningLevel(selectedReasoningLevel === "auto" ? null : selectedReasoningLevel);
        await refreshStatusAndProviders();
      }
      setModelPickerOpen(false);
    } catch (error) {
      setBannerMessage(formatErrorMessage(error));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleResetSessionModel() {
    const client = clientRef.current;
    const session = currentSessionRef.current;
    if (!client || !session) {
      return;
    }
    setBusyAction("switch-provider");
    try {
      const result = await client.setSessionModel(session.id, null, null);
      setCurrentSession(result.session);
      setBannerMessage(result.message);
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
      persistPromptHistory(next, selectedProjectPathRef.current);
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
      updateActiveProjectTurns((previous) => ({
        ...previous,
        [projectPath]: [
          ...(previous[projectPath] ?? []).filter((turn) => turn.sessionId !== session.id),
          { sessionId: session.id, turnId: operationId, baseMessageCount: session.messages.length },
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
    setSettingsSection(target);
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
  }

  function handleSelectSettingsSection(section: SettingsSectionKey) {
    setSettingsSection(section);
    if (section === "provider" || section === "mcp" || section === "hooks" || section === "system_prompt" || section === "runtime") {
      setSettingsConfigSection(section);
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
        for (const section of ["provider", "runtime", "mcp", "hooks", "system_prompt"] as SettingsConfigSectionKey[]) {
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

  function defaultProviderSetupDraft(preset?: ProviderPresetDescriptor | null, apiKey = "") {
    const providerName = normalizeProviderNameForToml(preset?.provider_name || "openai");
    const models = preset?.models?.length ? preset.models : ["gpt-4.1"];
    const defaultModel = preset?.default_model || models[0] || "gpt-4.1";
    const providerType = preset?.provider_type || "openai";
    const baseUrl = preset?.base_url || "https://api.openai.com/v1";
    return [
      "[providers]",
      `default = ${tomlString(providerName)}`,
      "",
      `[providers.${providerName}]`,
      `provider_type = ${tomlString(providerType)}`,
      `models = [${models.map(tomlString).join(", ")}]`,
      `default_model = ${tomlString(defaultModel)}`,
      `api_key = ${tomlString(apiKey)}`,
      `base_url = ${tomlString(baseUrl)}`,
      "",
    ].join("\n");
  }

  function applyProviderSetupPreset(preset: ProviderPresetDescriptor | null) {
    if (!preset) {
      setProviderSetupSelectedPreset("");
      setProviderSetupProviderName("custom-provider");
      setProviderSetupProviderType("openai");
      setProviderSetupBaseUrl("https://api.openai.com/v1");
      setProviderSetupModelsText("gpt-4.1");
      setProviderSetupDefaultModel("gpt-4.1");
      return;
    }
    setProviderSetupSelectedPreset(preset.id);
    setProviderSetupProviderName(preset.provider_name);
    setProviderSetupProviderType(preset.provider_type);
    setProviderSetupBaseUrl(preset.base_url);
    setProviderSetupModelsText(preset.models.join(", "));
    setProviderSetupDefaultModel(preset.default_model || preset.models[0] || "");
  }

  function providerSetupFormDraft() {
    const models = providerSetupModelsText
      .split(/[,，、]/)
      .map((item) => item.trim())
      .filter(Boolean);
    const defaultModel = providerSetupDefaultModel.trim() || models[0] || "";
    const providerName = normalizeProviderNameForToml(providerSetupProviderName || providerSetupSelectedPreset || "provider");
    return [
      "[providers]",
      `default = ${tomlString(providerName)}`,
      "",
      `[providers.${providerName}]`,
      `provider_type = ${tomlString(providerSetupProviderType || "openai")}`,
      `models = [${models.map(tomlString).join(", ")}]`,
      `default_model = ${tomlString(defaultModel)}`,
      `api_key = ${tomlString(providerSetupApiKey)}`,
      `base_url = ${tomlString(providerSetupBaseUrl)}`,
      "",
    ].join("\n");
  }

  function providerSetupFormIsComplete() {
    const models = providerSetupModelsText
      .split(/[,，、]/)
      .map((item) => item.trim())
      .filter(Boolean);
    return Boolean(
      providerSetupProviderName.trim() &&
        providerSetupProviderType.trim() &&
        providerSetupBaseUrl.trim() &&
        providerSetupApiKey.trim() &&
        models.length > 0 &&
        (providerSetupDefaultModel.trim() || models[0]),
    );
  }

  function providerSetupDraftIsComplete(value: string) {
    const text = value.trim();
    const hasProviderTable = /\[providers\.[^\]\s]+\]/i.test(text);
    const apiKeyMatch = text.match(/^\s*api_key\s*=\s*"([^"]+)"\s*$/im);
    const defaultModelMatch = text.match(/^\s*default_model\s*=\s*"([^"]+)"\s*$/im);
    return Boolean(
      hasProviderTable &&
        apiKeyMatch?.[1]?.trim() &&
        apiKeyMatch[1].trim() !== "..." &&
        defaultModelMatch?.[1]?.trim(),
    );
  }

  async function openProviderSetupModal() {
    const client = clientRef.current;
    if (!client || providerSetupOpen || providerSetupLoading) {
      return;
    }
    setProviderSetupOpen(true);
    setProviderSetupLoading(true);
    setProviderSetupMessage("");
    setProviderSetupScope("user");
    setProviderSetupMode("preset");
    setProviderSetupApiKey("");
    try {
      const [payload, presets] = await Promise.all([client.getSettingsConfig(), client.listProviderPresets()]);
      setProviderSetupPresets(presets);
      applyProviderSetupPreset(presets[0] ?? null);
      const userScope = payload.scopes.find((item) => item.scope === "user");
      const projectScope = payload.scopes.find((item) => item.scope === "project");
      const userProviderConfig = userScope?.sections.provider?.trim() ?? "";
      const projectProviderConfig = projectScope?.sections.provider?.trim() ?? "";
      if (userProviderConfig) {
        setProviderSetupDraft(userProviderConfig);
        setProviderSetupScope("user");
      } else if (projectProviderConfig) {
        setProviderSetupDraft(projectProviderConfig);
        setProviderSetupScope("project");
      } else {
        setProviderSetupDraft(defaultProviderSetupDraft(presets[0] ?? null));
        setProviderSetupScope("user");
      }
    } catch (error) {
      setProviderSetupPresets([]);
      applyProviderSetupPreset(null);
      setProviderSetupDraft(defaultProviderSetupDraft());
      setProviderSetupScope("user");
      setProviderSetupMessage(formatErrorMessage(error));
    } finally {
      setProviderSetupLoading(false);
    }
  }

  async function handleSaveProviderSetup() {
    const client = clientRef.current;
    if (!client) {
      setProviderSetupMessage(t("common.connectFirst"));
      return;
    }
    const content = providerSetupMode === "custom" ? providerSetupDraft : providerSetupFormDraft();
    const complete = providerSetupMode === "custom" ? providerSetupDraftIsComplete(content) : providerSetupFormIsComplete();
    if (!complete) {
      setProviderSetupMessage(t("providerSetup.validation"));
      return;
    }
    setProviderSetupSaving(true);
    setProviderSetupMessage("");
    try {
      await client.saveSettingsConfigSection(providerSetupScope, "provider", content);
      const refreshed = await refreshStatusAndProviders();
      await refreshSettingsConfig();
      if (!refreshed || refreshed.runtimeStatus.provider === "unconfigured" || refreshed.providerList.length === 0) {
        setProviderSetupMessage(t("providerSetup.stillMissing"));
        return;
      }
      setProviderSetupOpen(false);
      setProviderSetupMessage("");
    } catch (error) {
      setProviderSetupMessage(formatErrorMessage(error));
    } finally {
      setProviderSetupSaving(false);
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
      const visionResult =
        settingsConfigSection === "provider"
          ? await client.setVisionModel(
              selectedVisionProvider || null,
              selectedVisionProvider ? selectedVisionModel || null : null,
              settingsConfigScope,
            )
          : null;
      if (settingsConfigSection === "provider") {
        await refreshStatusAndProviders();
      }
      await refreshSettingsConfig();
      const configMessage =
        result.runtime_reloaded
          ? `Saved ${result.section} to ${result.config_path}. Runtime MCP tools are active now.`
          : `Saved ${result.section} to ${result.config_path}. Restart the sidecar to apply runtime changes.`;
      setSettingsConfigMessage(visionResult ? `${configMessage} ${visionResult.message}` : configMessage);
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

  async function handleDebugProviderModel(providerName: string, model: string): Promise<{ ok: boolean; message: string }> {
    const client = clientRef.current;
    if (!client) {
      throw new Error("Connect to a sidecar before testing provider models.");
    }
    return client.debugModelConnection(providerName, model);
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

  async function handleOpenProjectWorkspace(path: string) {
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
        await hideMainWindow();
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
        clearLastOpenedSession(projectPath);
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
  const activeProjectTurnList = selectedProjectPath ? (activeProjectTurns[selectedProjectPath] ?? []) : [];
  const currentSessionTurn = currentSession ? activeProjectTurnList.find((turn) => turn.sessionId === currentSession.id) ?? null : null;
  const conversationRows = buildConversationRows(
    currentSession,
    activeRuntimeConversationItems,
    activePendingTurn,
    currentSessionTurn ? currentSessionTurn.baseMessageCount : null,
  );
  const latestStreamingAssistantRowId =
    [...conversationRows].reverse().find((row) => row.role === "assistant" && row.isStreaming)?.id ?? null;
  const currentSessionInteraction = currentSession ? findSessionInteraction(pendingInteractions, currentSession.id) : null;
  const latestConversationRowId = conversationRows.length > 0 ? conversationRows[conversationRows.length - 1].id : "";
  const currentSessionRunning = currentSession ? activeProjectTurnList.some((turn) => turn.sessionId === currentSession.id) : false;
  const projectTurnLimitReached = activeProjectTurnList.length >= 2;
  const activeModelLabel = status?.model ?? selectedModel ?? t("composer.model");
  // The model trigger shows the model the *current* session will actually use:
  // its per-session pin if set, otherwise the workspace-wide default.
  const sessionModelPinned = Boolean(currentSession?.provider_override && currentSession?.model_override);
  const sessionModelLabel = currentSession?.model_override ?? status?.model ?? activeModelLabel;
  // Remote mode surfaces reconnect semantics on the connection indicator;
  // desktop keeps the raw state string.
  const connectionStateLabel = remoteMode ? t(remoteConnectionStateKey(connectionState)) : connectionState;
  const activeExecutionMode = normalizeExecutionMode(status?.execution_mode);
  const activeModeOption = EXECUTION_MODE_OPTIONS.find((mode) => mode.key === activeExecutionMode);
  const activeExecutionModeLabel =
    status?.execution_mode_title ?? (activeModeOption ? t(activeModeOption.titleKey) : t("common.executionModeUnavailable"));
  const contextUsage = currentSession?.context_window_usage ?? null;
  const contextPercent = normalizeContextPercent(contextUsage?.usage_percent);
  const contextColor = contextUsageColor(contextPercent);
  const contextFill = contextPercent ?? 0;
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
  const cacheUsage = buildCacheUsageSummary(currentSession?.token_usage);
  const cacheLabel = cacheUsage ? `Cache ${formatPercent(cacheUsage.ratio)}` : "Cache --";
  const cacheTitle = cacheUsage
    ? `Cache hit: ${formatPercent(cacheUsage.ratio)} (${formatTokenCount(cacheUsage.cacheReadTokens)} read / ${formatTokenCount(
        cacheUsage.promptTokens,
      )} prompt)`
    : "Session cache hit unavailable";
  const commandSuggestions = currentCommandSuggestions(draft);
  const conversationPreview = currentSession ? buildSessionPreview(currentSession) : "";
  const conversationTitle = truncateTopic(conversationPreview || selectedSessionId || t("conversation.newConversation"));
  const todoSummary = currentSession ? buildTodoSummary(currentSession.todo_items) : null;
  const todoLayoutKey =
    todoSummary?.visibleItems
      .map((item) => `${normalizeTodoStatus(item.status)}:${formatTodoLabel(item)}`)
      .join("|") ?? "";
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
  const selectedWorkerActive =
    selectedWorkerView !== null && activeConversationKey !== null && selectedWorkerView.conversationKey === activeConversationKey;
  const selectedWorkerMember = selectedWorkerActive
    ? activeTeamItems.find((member) => String(member.name) === selectedWorkerView.name) ?? null
    : null;
  const workerRefreshKey = selectedWorkerActive
    ? activeTeamItems
        .map((member) => `${String(member.name)}:${String(member.status ?? "")}:${String(member.activity ?? "")}:${String(member.current_tool_name ?? "")}:${String(member.current_task_id ?? "")}:${(member.recent_interactions ?? []).join("\u0001")}`)
        .join("\u0002")
    : "";

  conversationVirtualCountRef.current =
    conversationRows.length + (activeQueuedPrompts.length > 0 ? 1 : 0) + (currentSessionInteraction ? 1 : 0) + 1;

  useLayoutEffect(() => {
    if (conversationPinnedToBottomRef.current) {
      scrollConversationBodyToBottom();
    }
  }, [
    activePendingTurn?.placeholderText,
    activePendingTurn?.userText,
    activeQueuedPrompts.length,
    conversationRows.length,
    currentSessionInteraction?.id,
    draft,
    latestConversationRowId,
    latestStreamingAssistantRowId,
    pendingImages.length,
    runtimeConversationItems,
    todoExpanded,
    todoLayoutKey,
  ]);

  useEffect(() => {
    if (!selectedWorkerView) {
      return;
    }
    if (!activeConversationKey || selectedWorkerView.conversationKey !== activeConversationKey) {
      setSelectedWorkerView(null);
      setWorkerLogState({ loading: false, error: null, log: null });
      return;
    }
    const stillActive = activeTeamItems.some((member) => String(member.name) === selectedWorkerView.name);
    if (!stillActive) {
      setSelectedWorkerView(null);
      setWorkerLogState({ loading: false, error: null, log: null });
    }
  }, [activeConversationKey, activeTeamItems, selectedWorkerView]);

  useEffect(() => {
    if (!selectedWorkerActive || !selectedWorkerView) {
      return;
    }
    const client = clientRef.current;
    if (!client) {
      return;
    }
    let cancelled = false;
    setWorkerLogState((previous) => ({ ...previous, loading: true, error: null }));
    client
      .getTeamLog(selectedWorkerView.name, selectedWorkerView.sessionId)
      .then((log) => {
        if (!cancelled) {
          setWorkerLogState({ loading: false, error: null, log });
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setWorkerLogState({ loading: false, error: error instanceof Error ? error.message : String(error), log: null });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedWorkerActive, selectedWorkerView?.name, selectedWorkerView?.sessionId, workerRefreshKey]);

  if (remoteMode && !remoteConnected) {
    if (remoteRestorePending) {
      return (
        <main className="remote-shell remote-shell-login">
          <div className="remote-login">
            <h1>{t("remote.title")}</h1>
            <div className="remote-notice" role="status">
              {t("remote.restoring")}
            </div>
          </div>
        </main>
      );
    }
    if (!remoteAccess.authenticated) {
      // A `#/pair` deep link stays on its hash while signed out: the sign-in
      // form renders in place and the router resolves back to the pair page
      // once authenticated — no return-URL bookkeeping needed.
      return remoteRoute === "register" ? <RemoteRegisterPage access={remoteAccess} /> : <RemoteLoginPage access={remoteAccess} />;
    }
    if (remoteRoute === "pair") {
      return <RemotePairPage relayUrl={remoteAccess.relayUrl} />;
    }
    return (
      <RemoteConnectPage
        access={remoteAccess}
        connecting={remoteConnecting}
        onConnect={(deviceId, projectId) => void connectRemoteProject(deviceId, projectId)}
        onSignOut={handleRemoteSignOut}
      />
    );
  }

  return (
    <div className="shell">
      <header
        className="app-titlebar"
        data-tauri-drag-region
        onPointerDown={(event) => void handleTitlebarPointerDown(event)}
        onDoubleClick={(event) => void handleTitlebarDoubleClick(event)}
      >
        {isMobile ? (
          <button
            className="titlebar-button titlebar-menu"
            type="button"
            onClick={() => setSidebarDrawerOpen((current) => !current)}
            title={t("titlebar.menu")}
            aria-label={t("titlebar.menu")}
            aria-expanded={sidebarDrawerOpen}
          >
            <span aria-hidden="true" />
          </button>
        ) : null}
        <div className="titlebar-brand" data-tauri-drag-region>
          <img className="titlebar-icon" src={appIconUrl} alt="" aria-hidden="true" data-tauri-drag-region />
          <span data-tauri-drag-region>{t("app.title")}</span>
        </div>
        <div className="titlebar-controls">
          <button className="titlebar-button" type="button" onClick={handleOpenSettings} title={t("settings.title")} aria-label={t("settings.title")}>
            ⚙
          </button>
          {!remoteMode ? (
            <>
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
            </>
          ) : null}
        </div>
      </header>
      <div className="ambient ambient-left" />
      <div className="ambient ambient-right" />
      {settingsOpen ? (
        <SettingsView
          activeSection={settingsSection}
          onSelectSection={handleSelectSettingsSection}
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
          onOpenPath={remoteMode ? null : handleOpenSettingsPath}
          configScopes={settingsConfigScopes}
          configDrafts={settingsConfigDrafts}
          mcpServers={settingsMcpServers}
          selectedConfigScope={settingsConfigScope}
          configLoading={settingsConfigLoading}
          configSaving={settingsConfigSaving}
          configMessage={settingsConfigMessage}
          onSelectConfigScope={setSettingsConfigScope}
          onConfigDraftChange={(key, value) => setSettingsConfigDrafts((previous) => ({ ...previous, [key]: value }))}
          onSaveConfigSection={handleSaveSettingsConfigSection}
          onDebugMcpServer={handleDebugMcpServer}
          onSetMcpServerEnabled={handleSetMcpServerEnabled}
          onDebugProviderModel={handleDebugProviderModel}
          onReloadConfig={refreshSettingsConfig}
          providers={providers}
          visionModels={visionModels}
          selectedVisionProvider={selectedVisionProvider}
          selectedVisionModel={selectedVisionModel}
          onSetVisionProviderDraft={(providerName) => void handleVisionProviderChange(providerName)}
          onSetVisionModelDraft={setSelectedVisionModel}
          remoteClient={remoteMode || !clientRef.current ? null : new SidecarClient(clientRef.current.baseUrl)}
          collectRemoteProjects={remoteMode ? null : collectRemoteProjects}
        />
      ) : null}
      {providerSetupOpen ? (
        <div className="provider-setup-backdrop" role="presentation">
          <section className="provider-setup-modal" role="dialog" aria-modal="true" aria-labelledby="provider-setup-title">
            <header className="provider-setup-header">
              <div>
                <h1 id="provider-setup-title">{t("providerSetup.title")}</h1>
                <p>{t("providerSetup.subtitle")}</p>
              </div>
            </header>
            <div className="provider-setup-body">
              {providerSetupLoading ? (
                <div className="settings-empty-state">
                  <p>{t("settings.config.loading")}</p>
                </div>
              ) : (
                <ProviderSetupForm
                  mode={providerSetupMode}
                  presets={providerSetupPresets}
                  selectedPreset={providerSetupSelectedPreset}
                  scope={providerSetupScope}
                  providerName={providerSetupProviderName}
                  providerType={providerSetupProviderType}
                  baseUrl={providerSetupBaseUrl}
                  apiKey={providerSetupApiKey}
                  modelsText={providerSetupModelsText}
                  defaultModel={providerSetupDefaultModel}
                  customDraft={providerSetupDraft}
                  onModeChange={setProviderSetupMode}
                  onScopeChange={setProviderSetupScope}
                  onPresetChange={(presetId) => {
                    const preset = providerSetupPresets.find((item) => item.id === presetId) ?? null;
                    applyProviderSetupPreset(preset);
                    setProviderSetupDraft(defaultProviderSetupDraft(preset));
                  }}
                  onProviderNameChange={setProviderSetupProviderName}
                  onProviderTypeChange={setProviderSetupProviderType}
                  onBaseUrlChange={setProviderSetupBaseUrl}
                  onApiKeyChange={setProviderSetupApiKey}
                  onModelsTextChange={(value) => {
                    setProviderSetupModelsText(value);
                    const models = value.split(/[,，、]/).map((item) => item.trim()).filter(Boolean);
                    if (!providerSetupDefaultModel || (models.length > 0 && !models.includes(providerSetupDefaultModel))) {
                      setProviderSetupDefaultModel(models[0] ?? "");
                    }
                  }}
                  onDefaultModelChange={setProviderSetupDefaultModel}
                  onCustomDraftChange={setProviderSetupDraft}
                />
              )}
            </div>
            <footer className="provider-setup-footer">
              {providerSetupMessage ? <span className="provider-setup-message">{providerSetupMessage}</span> : <span />}
              <button
                className="settings-inline-button"
                type="button"
                onClick={() => void handleSaveProviderSetup()}
                disabled={
                  providerSetupLoading ||
                  providerSetupSaving ||
                  (providerSetupMode === "custom" ? !providerSetupDraftIsComplete(providerSetupDraft) : !providerSetupFormIsComplete())
                }
              >
                {providerSetupSaving ? t("settings.config.saving") : t("providerSetup.save")}
              </button>
            </footer>
          </section>
        </div>
      ) : null}
      <main
        ref={workspaceRef}
        className={`workspace ${contextPanelOpen ? "context-open" : "context-collapsed"} ${layoutDragging ? "resizing" : ""} ${isMobile ? "mobile" : ""}`}
        style={workspaceStyle}
      >
        {isMobile && (sidebarDrawerOpen || contextPanelOpen) ? (
          <div
            className="drawer-scrim"
            onClick={() => {
              setSidebarDrawerOpen(false);
              if (contextPanelOpen) {
                setContextPanelOpen(false);
              }
            }}
            aria-hidden="true"
          />
        ) : null}
        <aside className={`panel sidebar-panel ${sidebarDrawerOpen ? "drawer-open" : ""}`}>
          <div className="panel-header">
            <div>
              <h2>{t("sidebar.projects")}</h2>
            </div>
            <div className="panel-header-actions">
              <span className="panel-count">{t("sidebar.total", { count: visibleProjectCount })}</span>
              {isMobile ? (
                <button
                  className="action ghost sidebar-close"
                  type="button"
                  onClick={() => setSidebarDrawerOpen(false)}
                  title={t("sidebar.close")}
                  aria-label={t("sidebar.close")}
                >
                  ×
                </button>
              ) : null}
              {remoteMode ? (
                <button
                  className="action primary sidebar-new"
                  onClick={handleRemoteSwitchTarget}
                  title={t("remote.switchTarget")}
                  aria-label={t("remote.switchTarget")}
                >
                  ⇄
                </button>
              ) : (
                <button
                  className="action primary sidebar-new"
                  onClick={() => void handleCreateProject()}
                  disabled={busyAction !== null}
                  title={t("sidebar.newProject")}
                  aria-label={t("sidebar.newProject")}
                >
                  +
                </button>
              )}
            </div>
          </div>

          {projectLimitNotice ? <div className="sidebar-notice">{projectLimitNotice}</div> : null}

          <div className="session-list">
            {sessionProjectGroups.length === 0 ? (
              <div className="empty-card">
                <p>{t("sidebar.noProjects")}</p>
                <span>{t("sidebar.noProjectsHint")}</span>
              </div>
            ) : (
              <div className="project-groups">
                {sessionProjectGroups.map((group) => {
                  const isCollapsed = collapsedProjects[group.key] ?? true;
                  return (
                    <section key={group.key} className="project-group">
                      <div className={`project-toggle ${isCollapsed ? "collapsed" : ""}`}>
                        <button
                          className="project-toggle-button"
                          onClick={() =>
                            setCollapsedProjects((previous) => ({
                              ...previous,
                              [group.key]: !isCollapsed,
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
                              {!remoteMode ? (
                                <button
                                  className="project-menu-item"
                                  onClick={() => {
                                    setProjectMenuOpenKey(null);
                                    void handleOpenProjectWorkspace(group.path);
                                  }}
                                >
                                  {t("sidebar.openWorkspace")}
                                </button>
                              ) : null}
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
                              {!remoteMode ? (
                                <button
                                  className="project-menu-item danger"
                                  onClick={() => {
                                    void handleRemoveProject(group.path);
                                  }}
                                  disabled={busyAction !== null}
                                >
                                  {t("sidebar.removeProject")}
                                </button>
                              ) : null}
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
                            const sessionCacheUsage = buildCacheUsageSummary(session.token_usage);
                            return (
                              <div
                                key={session.id}
                                className={`session-card ${isSelected ? "selected" : ""} ${isAnswering ? "answering" : ""} ${isWaitingForDecision ? "waiting-decision" : ""} ${sessionMenuOpenKey === sessionMenuKey ? "menu-open" : ""}`}
                              >
                                <button
                                  className="session-card-button"
                                  onClick={() => {
                                    setSidebarDrawerOpen(false);
                                    void activateProject(group.path, projectClientsRef.current[group.path]).then(() =>
                                      selectSession(session.id, projectClientsRef.current[group.path], undefined, group.path),
                                    );
                                  }}
                                >
                                  <div className="session-card-head">
                                    <strong>{session.id}</strong>
                                  </div>
                                  <p>{buildSessionPreview(session)}</p>
                                  {sessionCacheUsage ? (
                                    <span className="session-card-cache">
                                      Cache {formatPercent(sessionCacheUsage.ratio)} · {formatTokenCount(sessionCacheUsage.cacheReadTokens)} read
                                    </span>
                                  ) : null}
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
              {remoteMode ? (
                <span className="workspace-link workspace-link-static" title={workspaceRootPath || t("conversation.workspaceUnavailable")}>
                  {workspaceRootName}
                </span>
              ) : (
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
              )}
            </div>
            <div className="status-cluster">
              {selectedWorkerActive ? (
                <button
                  className="action ghost"
                  type="button"
                  onClick={() => {
                    setSelectedWorkerView(null);
                    setWorkerLogState({ loading: false, error: null, log: null });
                  }}
                  title={t("worker.backToLead")}
                  aria-label={t("worker.backToLead")}
                >
                  {t("worker.backToLead")}
                </button>
              ) : null}
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

          {!selectedWorkerActive ? (
            <TodoStatusBar summary={todoSummary} expanded={todoExpanded} onToggleExpanded={() => setTodoExpanded((current) => !current)} />
          ) : null}

          <div className="conversation-body">
            {selectedWorkerActive ? (
              <div className="conversation-content">
                <WorkerOutputView member={selectedWorkerMember} workerName={selectedWorkerView?.name ?? ""} state={workerLogState} />
              </div>
            ) : conversationRows.length === 0 && activeQueuedPrompts.length === 0 && !currentSessionInteraction ? (
              <div className="conversation-content">
                <div className="empty-conversation">
                  <h3>{t("conversation.startSession")}</h3>
                  <p>{t("conversation.startSessionHint")}</p>
                </div>
              </div>
            ) : (
              <VList
                ref={conversationListRef}
                className="conversation-virtual-list"
                bufferSize={400}
                onScroll={() => {
                  const list = conversationListRef.current;
                  if (!list) {
                    return;
                  }
                  conversationPinnedToBottomRef.current =
                    list.scrollSize - list.viewportSize - list.scrollOffset <= CONVERSATION_BOTTOM_STICKY_THRESHOLD;
                }}
              >
                {conversationRows.map((row, index) => (
                  <div key={row.id} className={`conversation-virtual-item${index === 0 ? " first" : ""}`}>
                    <article className={`bubble ${row.role} ${row.isPending ? "pending" : ""}`}>
                      {row.parts?.length ? (
                        row.parts.map((part) =>
                          part.type === "text" ? (
                            <MarkdownMessage key={part.id} text={part.text} />
                          ) : part.type === "thinking_log" ? (
                            <ThinkingLogPanel key={part.id} thinkingLog={part.thinkingLog} client={clientRef.current} />
                          ) : (
                            <div key={part.id} className="tool-call-stack">
                              <ToolCallWithImages
                                toolCall={part.toolCall}
                                client={clientRef.current}
                                onPreviewImage={setToolImagePreview}
                              />
                            </div>
                          ),
                        )
                      ) : row.text ? (
                        <MarkdownMessage text={row.text} />
                      ) : null}
                      {row.images?.length ? (
                        <div className="user-image-list">
                          {row.images.map((image, index) => (
                            <UserImagePreview
                              key={`${image.path ?? image.absolute_path ?? image.image_url ?? `img-${index}`}`}
                              image={image}
                              index={index}
                              client={clientRef.current}
                              onPreviewImage={setToolImagePreview}
                            />
                          ))}
                        </div>
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
                            <ToolCallWithImages
                              key={toolCall.id}
                              toolCall={toolCall}
                              client={clientRef.current}
                              onPreviewImage={setToolImagePreview}
                            />
                          ))}
                        </div>
                      ) : null}
                      {row.id === latestStreamingAssistantRowId && currentSessionRunning ? (
                        <span className="session-answering-indicator conversation-answering-indicator" aria-label={t("sidebar.agentResponding")}>
                          <span aria-hidden="true" />
                          <span aria-hidden="true" />
                          <span aria-hidden="true" />
                        </span>
                      ) : null}
                    </article>
                  </div>
                ))}
                {activeQueuedPrompts.length > 0 ? (
                  <div key="prompt-queue" className="conversation-virtual-item">
                    <PromptQueueCard
                      prompts={activeQueuedPrompts}
                      canInject={currentSessionRunning}
                      busy={busyAction !== null}
                      onInject={handleQueuePromptInjection}
                    />
                  </div>
                ) : null}
                {currentSessionInteraction ? (
                  <div key="session-interaction" className="conversation-virtual-item">
                    <InteractionDecisionCard
                      interaction={currentSessionInteraction}
                      busy={busyAction !== null}
                      onResolveAuthorization={handleResolveAuthorization}
                      onResolveModeSwitch={handleResolveModeSwitch}
                    />
                  </div>
                ) : null}
                <div key="conversation-end" className="conversation-end" aria-hidden="true" />
              </VList>
            )}
          </div>

          {!selectedWorkerActive ? (
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
                      title={
                        sessionModelPinned
                          ? t("composer.modelPinnedTooltip")
                          : t("composer.modelDefaultTooltip")
                      }
                    >
                      <span>{sessionModelLabel}</span>
                      <span
                        className={`connection-dot ${connectionState === "connected" ? "connected" : "attention"}`}
                        aria-label={connectionStateLabel}
                        title={connectionStateLabel}
                      />
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
                          {sessionModelPinned ? (
                            <button
                              className="action secondary picker-reset"
                              onClick={() => void handleResetSessionModel()}
                              disabled={busyAction !== null}
                              title={t("composer.resetModelTooltip")}
                            >
                              {t("composer.resetModel")}
                            </button>
                          ) : null}
                          <button
                            className="action secondary picker-apply"
                            onClick={() => void handleApplyProviderModel()}
                            disabled={
                              !selectedProvider ||
                              !selectedModel ||
                              busyAction !== null
                            }
                            title={
                              currentSession
                                ? t("composer.applySessionTooltip")
                                : t("composer.apply")
                            }
                          >
                            {currentSession ? t("composer.applySession") : t("composer.apply")}
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
                          <span>Cache hit</span>
                          <strong title={cacheTitle}>{cacheLabel}</strong>
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
          ) : null}
        </section>

        {taskGraphPanelOpen ? (
          <TaskGraphWorkspacePanel tasks={activeTaskItems} onClose={() => setTaskGraphPanelOpen(false)} />
        ) : null}

        {toolImagePreview ? <ToolImageLightbox preview={toolImagePreview} onClose={() => setToolImagePreview(null)} /> : null}

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
                    <span>Cache hit</span>
                    <strong title={cacheTitle}>{cacheLabel}</strong>
                  </div>
                  <div className="fact-row">
                    <span>Current mode</span>
                    <strong>{status?.execution_mode_title ?? "unknown"}</strong>
                  </div>
                  <TaskGraphPanel tasks={activeTaskItems} onOpenPanel={() => setTaskGraphPanelOpen(true)} />
                  {activeSubagentItems.length > 0 || activeTeamItems.length > 0 ? (
                    <ExecutionActivityPanel
                      subagents={activeSubagentItems}
                      teamMembers={activeTeamItems}
                      selectedTeamMemberName={selectedWorkerActive ? selectedWorkerView?.name ?? null : null}
                      onSelectTeamMember={(member) => {
                        if (!activeConversationKey) {
                          return;
                        }
                        setSelectedWorkerView({
                          conversationKey: activeConversationKey,
                          name: String(member.name),
                          sessionId: typeof member.session_id === "string" ? member.session_id : currentSession?.id ?? null,
                        });
                      }}
                    />
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
  const interactive = sortedTasks.length > 0;

  return (
    <section
      className={`task-graph-panel ${interactive ? "interactive" : "disabled"}`}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-label={interactive ? t("taskGraph.expand") : undefined}
      onClick={interactive ? onOpenPanel : undefined}
      onKeyDown={
        interactive
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onOpenPanel();
              }
            }
          : undefined
      }
    >
      <div className="task-graph-head">
        <div>
          <h3>{t("taskGraph.title")}</h3>
          <p>
            {counts.total === 0
              ? t("taskGraph.empty")
              : t("taskGraph.summary", { completed: counts.completed, total: counts.total, inProgress: counts.inProgress, pending: counts.pending })}
          </p>
        </div>
        <button
          className="task-graph-expand-button"
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onOpenPanel();
          }}
          disabled={!interactive}
          title={t("taskGraph.expand")}
          aria-label={t("taskGraph.expand")}
        >
          <svg viewBox="0 0 1024 1024" aria-hidden="true">
            <path d="M853.333333 213.333333a42.666667 42.666667 0 0 0-42.666666-42.666666h-213.333334a42.666667 42.666667 0 0 0 0 85.333333h109.653334l-139.946667 140.373333a42.666667 42.666667 0 0 0 0 60.586667 42.666667 42.666667 0 0 0 60.586667 0L768 316.586667V426.666667a42.666667 42.666667 0 0 0 42.666667 42.666666 42.666667 42.666667 0 0 0 42.666666-42.666666zM456.96 567.04a42.666667 42.666667 0 0 0-60.586667 0L256 706.986667V597.333333a42.666667 42.666667 0 0 0-42.666667-42.666666 42.666667 42.666667 0 0 0-42.666666 42.666666v213.333334a42.666667 42.666667 0 0 0 42.666666 42.666666h213.333334a42.666667 42.666667 0 0 0 0-85.333333H316.586667l140.373333-140.373333a42.666667 42.666667 0 0 0 0-60.586667z" />
          </svg>
        </button>
      </div>
      {sortedTasks.length === 0 ? (
        <div className="task-graph-empty">{t("taskGraph.hint")}</div>
      ) : (
        <div className="task-graph-canvas">
          <TaskGraphSvg graph={graph} selectedTaskId={null} onSelectTask={() => {}} compact />
        </div>
      )}
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
    <svg
      className={`task-graph-svg ${compact ? "compact" : ""}`}
      viewBox={`0 0 ${graph.width} ${graph.height}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="Task dependency graph"
    >
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
      </dl>
    </section>
  );
}

function ExecutionActivityPanel({
  subagents,
  teamMembers,
  selectedTeamMemberName,
  onSelectTeamMember,
}: {
  subagents: SubagentActivity[];
  teamMembers: TeamMemberActivity[];
  selectedTeamMemberName?: string | null;
  onSelectTeamMember?: (member: TeamMemberActivity) => void;
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
            const isSelected = selectedTeamMemberName === String(member.name);
            return (
              <button
                key={String(member.name)}
                className={`activity-item activity-item-button ${isSelected ? "selected" : ""}`}
                type="button"
                onClick={() => onSelectTeamMember?.(member)}
                aria-pressed={isSelected}
              >
                <div className="activity-item-head">
                  <span>{member.name}</span>
                  <em>{member.status ?? t("common.active")}</em>
                </div>
                <p>{teamMemberSummary(member)}</p>
                {interactions.length > 0 ? <small>{interactions[interactions.length - 1]}</small> : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function WorkerOutputView({
  member,
  workerName,
  state,
}: {
  member: TeamMemberActivity | null;
  workerName: string;
  state: WorkerLogState;
}) {
  const { t } = useI18n();
  const rendered = state.log?.rendered?.trim() ?? "";
  const entries = state.log?.entries ?? [];
  return (
    <section className="worker-output">
      <header className="worker-output-header">
        <div>
          <p className="panel-kicker">{t("worker.label")}</p>
          <h3>{workerName}</h3>
        </div>
        <span>{member?.status ?? t("common.active")}</span>
      </header>
      {member ? <p className="worker-output-summary">{teamMemberSummary(member)}</p> : null}
      {state.error ? <div className="empty-card">{state.error}</div> : null}
      {state.loading && !rendered ? (
        <span className="typing-indicator" aria-label={t("conversation.waitingAssistant")}>
          <span />
          <span />
          <span />
        </span>
      ) : null}
      {entries.length > 0 ? (
        <div className="worker-log-events">
          {entries.map((entry, index) => (
            <WorkerLogEntryCard key={`${String(entry.type ?? "event")}-${index}`} entry={entry} index={index} />
          ))}
        </div>
      ) : rendered ? (
        <pre className="worker-output-log">{rendered}</pre>
      ) : !state.loading && !state.error ? (
        <div className="empty-card">{t("worker.noOutput")}</div>
      ) : null}
    </section>
  );
}

function WorkerLogEntryCard({ entry, index }: { entry: TeamLogEntry; index: number }) {
  const eventType = String(entry.type ?? "event");
  if (eventType === "user_message") {
    return (
      <article className="bubble user worker-log-bubble">
        <div className="worker-log-meta">
          <span>{entry.source ? `user / ${entry.source}` : "user"}</span>
          <em>{formatWorkerLogTimestamp(entry.timestamp)}</em>
        </div>
        <MarkdownMessage text={renderWorkerLogContent(entry.content)} />
      </article>
    );
  }
  if (eventType === "assistant_message") {
    return (
      <article className="bubble assistant worker-log-bubble">
        <div className="worker-log-meta">
          <span>assistant</span>
          <em>{formatWorkerLogTimestamp(entry.timestamp)}</em>
        </div>
        <MarkdownMessage text={renderWorkerLogContent(entry.content)} />
      </article>
    );
  }
  if (eventType === "tool_call") {
    const toolCall: ConversationToolCall = {
      id: entry.tool_log_id || `worker-tool-${index}`,
      name: String(entry.tool_name ?? "tool"),
      input: stringifyToolValue(entry.tool_input ?? {}),
      output: String(entry.output_preview ?? "(no output)"),
      rawInput: entry.tool_input ?? {},
      rawOutput: entry.output_preview ?? "(no output)",
      logId: typeof entry.tool_log_id === "string" ? entry.tool_log_id : null,
      status: "finished",
    };
    return (
      <div className="worker-log-tool">
        <ToolCallCard toolCall={toolCall} />
      </div>
    );
  }
  if (eventType === "runtime_error") {
    return (
      <article className="worker-log-event error">
        <div className="worker-log-meta">
          <span>runtime_error</span>
          <em>{formatWorkerLogTimestamp(entry.timestamp)}</em>
        </div>
        <pre>{String(entry.error ?? "unknown error")}</pre>
      </article>
    );
  }
  return (
    <article className="worker-log-event">
      <div className="worker-log-meta">
        <span>{workerLogEventLabel(entry)}</span>
        <em>{formatWorkerLogTimestamp(entry.timestamp)}</em>
      </div>
      <pre>{stringifyToolValue(entry)}</pre>
    </article>
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

// Cache parsed markdown per message text so re-renders (streaming, composer
// keystrokes) do not re-parse the whole history. Bounded to cap memory use.
const markdownRenderCache = new Map<string, ReactNode>();

function renderMarkdownBlocksCached(text: string): ReactNode {
  const cached = markdownRenderCache.get(text);
  if (cached !== undefined) {
    return cached;
  }
  const rendered = renderMarkdownBlocks(text);
  if (markdownRenderCache.size >= 200) {
    markdownRenderCache.clear();
  }
  markdownRenderCache.set(text, rendered);
  return rendered;
}

const MarkdownMessage = memo(function MarkdownMessage({ text }: { text: string }) {
  return <div className="markdown-content">{renderMarkdownBlocksCached(text)}</div>;
});

function ThinkingLogPanel({ thinkingLog, client }: { thinkingLog: ConversationThinkingLog; client: SomniaClient | null }) {
  const bodyRef = useRef<HTMLPreElement | null>(null);
  const isRunning = thinkingLog.status === "running";
  const path = thinkingLog.path?.trim() ?? "";
  const incomingText = thinkingLog.text ?? "";
  const [expanded, setExpanded] = useState(isRunning);
  const [loadedText, setLoadedText] = useState(incomingText);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const text = loadedText.trim();

  useEffect(() => {
    setExpanded(isRunning);
    setLoadedText(incomingText);
    setLoading(false);
    setLoadError(null);
  }, [incomingText, isRunning, path]);

  useEffect(() => {
    if (!expanded || isRunning || text || !path || !client) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    client
      .getThinkingLog(path)
      .then((detail) => {
        if (cancelled) {
          return;
        }
        setLoadedText(detail.text ?? "");
      })
      .catch((error) => {
        if (cancelled) {
          return;
        }
        setLoadError(formatErrorMessage(error));
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, expanded, isRunning, path, text]);

  useEffect(() => {
    if (!expanded || !bodyRef.current) {
      return;
    }
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [expanded, isRunning, text]);

  return (
    <details
      className={`thinking-log-card ${isRunning ? "running" : "finished"}`}
      open={expanded}
      onToggle={(event) => {
        const nextOpen = event.currentTarget.open;
        setExpanded(isRunning ? true : nextOpen);
      }}
    >
      <summary>
        <span className="thinking-log-summary-main">
          <span className={`tool-result-dot ${isRunning ? "running" : "success"}`} aria-hidden="true" />
          <span>Thinking</span>
        </span>
      </summary>
      {text ? (
        <pre ref={bodyRef} className="thinking-log-body">
          {loadedText}
        </pre>
      ) : loading ? (
        <div className="thinking-log-detail">
          <span>Loading thinking log...</span>
        </div>
      ) : loadError ? (
        <div className="thinking-log-detail error">
          <span>{loadError}</span>
        </div>
      ) : (
        <div className="thinking-log-detail">
          <span>Thinking log is available after the turn finishes.</span>
        </div>
      )}
    </details>
  );
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

function ToolCallWithImages({
  toolCall,
  client,
  onPreviewImage,
}: {
  toolCall: ConversationToolCall;
  client: SomniaClient | null;
  onPreviewImage: (preview: ToolImagePreviewState) => void;
}) {
  const imageReferences = toolCall.contentBlocks?.filter((block) => block.type === "image_reference") ?? [];
  return (
    <>
      <ToolCallCard toolCall={toolCall} />
      {imageReferences.length > 0 ? (
        <div className="tool-image-list">
          {imageReferences.map((image, index) => (
            <ToolImagePreview
              key={`${image.path ?? image.absolute_path ?? image.image_url ?? "image"}-${index}`}
              image={image}
              index={index}
              client={client}
              onPreviewImage={onPreviewImage}
            />
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

function UserImagePreview({
  image,
  index,
  client,
  onPreviewImage,
}: {
  image: import("./types").ConversationImageReferenceBlock;
  index: number;
  client: SomniaClient | null;
  onPreviewImage: (preview: ToolImagePreviewState) => void;
}) {
  const src = useWorkspaceImageSource(image, client);
  if (!src) {
    return null;
  }
  return (
    <button className="tool-image-preview" type="button" title={image.path ?? image.absolute_path ?? `Image ${index + 1}`} onClick={() => onPreviewImage({ src, label: image.path ?? `Image ${index + 1}` })}>
      <img src={src} alt={`User image ${index + 1}`} loading="lazy" />
    </button>
  );
}

function ToolImagePreview({
  image,
  index,
  client,
  onPreviewImage,
}: {
  image: NonNullable<ConversationToolCall["contentBlocks"]>[number] & { type: "image_reference" };
  index: number;
  client: SomniaClient | null;
  onPreviewImage: (preview: ToolImagePreviewState) => void;
}) {
  const src = useWorkspaceImageSource(image, client);
  const labels = toolImageLabels(image, index);
  if (!src) {
    return <span className="tool-image-missing" title={labels.title}>{labels.display}</span>;
  }
  return (
    <button className="tool-image-preview" type="button" title={labels.title} onClick={() => onPreviewImage({ src, label: labels.display })}>
      <img src={src} alt={labels.display} loading="lazy" />
      <span>{labels.display}</span>
    </button>
  );
}

function ToolImageLightbox({ preview, onClose }: { preview: ToolImagePreviewState; onClose: () => void }) {
  const [scale, setScale] = useState(1);
  const [transformOrigin, setTransformOrigin] = useState("50% 50%");

  function handleWheel(event: ReactWheelEvent<HTMLImageElement>) {
    event.preventDefault();
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width > 0 && bounds.height > 0) {
      const originX = ((event.clientX - bounds.left) / bounds.width) * 100;
      const originY = ((event.clientY - bounds.top) / bounds.height) * 100;
      setTransformOrigin(`${clampPercent(originX)}% ${clampPercent(originY)}%`);
    }
    const direction = event.deltaY < 0 ? 1 : -1;
    setScale((current) => clampToolImageScale(current + direction * TOOL_IMAGE_SCALE_STEP));
  }

  return (
    <div className="tool-image-lightbox" role="dialog" aria-modal="true" aria-label={preview.label} onClick={onClose}>
      <div className="tool-image-lightbox-content">
        <img
          src={preview.src}
          alt={preview.label}
          style={{ transform: `scale(${scale})`, transformOrigin }}
          onWheel={handleWheel}
          onClick={onClose}
        />
        <span>{preview.label}</span>
      </div>
    </div>
  );
}

function clampToolImageScale(value: number): number {
  return Math.min(TOOL_IMAGE_MAX_SCALE, Math.max(TOOL_IMAGE_MIN_SCALE, Number(value.toFixed(2))));
}

function clampPercent(value: number): number {
  return Math.min(100, Math.max(0, Number(value.toFixed(2))));
}

function toolImageLabels(image: { path?: string; absolute_path?: string; image_url?: string }, index: number): { display: string; title: string } {
  const source = String(image.path || image.absolute_path || image.image_url || "").trim();
  const fallback = `Tool image ${index + 1}`;
  if (!source) {
    return { display: fallback, title: fallback };
  }
  if (/^data:image\//i.test(source)) {
    const mediaType = /^data:([^;,]+)[;,]/i.exec(source)?.[1] ?? "image";
    return { display: `${fallback} (${mediaType})`, title: source };
  }
  if (/^https?:\/\//i.test(source)) {
    return { display: compactImageUrlLabel(source, fallback), title: source };
  }
  return { display: fileNameFromPath(source) || compactInlineText(source, 42) || fallback, title: source };
}

function fileNameFromPath(path: string): string {
  return path
    .replace(/\\/g, "/")
    .split("/")
    .filter(Boolean)
    .pop()
    ?.trim() ?? "";
}

function compactImageUrlLabel(value: string, fallback: string): string {
  try {
    const url = new URL(value);
    const fileName = fileNameFromPath(url.pathname);
    return fileName ? `${url.hostname}/${fileName}` : url.hostname || fallback;
  } catch {
    return compactInlineText(value, 42) || fallback;
  }
}

function toolResultContentBlocksFromEvent(payload: Record<string, unknown>): ConversationContentBlock[] {
  const contents: unknown[] = [payload.content_blocks];
  if (isRecord(payload.output)) {
    contents.push(payload.output.content_blocks, payload.output.tool_result_content);
  }
  return normalizeToolContentBlocks(...contents);
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

function readRemoteRelayDefault(): string {
  if (typeof window === "undefined") {
    return DEFAULT_REMOTE_RELAY_URL;
  }
  const override = new URLSearchParams(window.location.search).get("relay");
  if (override) {
    return override;
  }
  // Deployed stacks serve the Web app and the Relay behind the same origin,
  // so non-loopback hosts talk to their own origin. Local preview servers
  // run the Relay separately, which keeps the loopback default below.
  const hostname = window.location.hostname.toLowerCase();
  if (hostname !== "127.0.0.1" && hostname !== "localhost" && hostname !== "::1" && hostname !== "[::1]") {
    return window.location.origin;
  }
  return DEFAULT_REMOTE_RELAY_URL;
}

function remoteConnectionStateKey(
  state: "connecting" | "connected" | "disconnected" | "error",
): "remote.state.connecting" | "remote.state.connected" | "remote.state.disconnected" | "remote.state.error" {
  switch (state) {
    case "connected":
      return "remote.state.connected";
    case "connecting":
      return "remote.state.connecting";
    case "error":
      return "remote.state.error";
    default:
      return "remote.state.disconnected";
  }
}

function waitForConnectionOpen(client: SomniaClient): Promise<void> {
  if (client.connectionState() === "connected") {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const unsubscribe = client.subscribe((notification) => {
      if (notification.kind !== "state") {
        return;
      }
      if (notification.state === "connected") {
        unsubscribe();
        resolve();
      } else if (notification.state === "error") {
        unsubscribe();
        reject(new Error(notification.error ?? "Remote connection failed."));
      }
    });
  });
}

/**
 * Remote projects bucket their last-opened-session memory per
 * device/project under `somnia.remote.last-opened-session:<deviceId>:<projectId>`;
 * desktop projects keep the unchanged global desktop key.
 */
function lastOpenedSessionStorageKey(projectPath?: string | null): string {
  return remoteScopedStorageKey("last-opened-session", projectPath) ?? LAST_OPENED_SESSION_STORAGE_KEY;
}

function readLastOpenedSession(projectPath?: string | null): LastOpenedSessionState | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const value = JSON.parse(window.localStorage.getItem(lastOpenedSessionStorageKey(projectPath)) ?? "null") as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    const projectPathValue = (value as { projectPath?: unknown }).projectPath;
    const sessionId = (value as { sessionId?: unknown }).sessionId;
    if (typeof projectPathValue !== "string" || !projectPathValue.trim() || typeof sessionId !== "string" || !sessionId.trim()) {
      return null;
    }
    return { projectPath: projectPathValue, sessionId };
  } catch {
    return null;
  }
}

function persistLastOpenedSession(projectPath: string, sessionId: string) {
  if (typeof window === "undefined" || !projectPath.trim() || !sessionId.trim()) {
    return;
  }
  window.localStorage.setItem(lastOpenedSessionStorageKey(projectPath), JSON.stringify({ projectPath, sessionId }));
}

function clearLastOpenedSession(projectPath?: string | null, sessionId?: string | null) {
  if (typeof window === "undefined") {
    return;
  }
  const storageKey = lastOpenedSessionStorageKey(projectPath);
  const current = readLastOpenedSession(projectPath);
  if (!current) {
    return;
  }
  const projectMatches = !projectPath || projectPathKey(current.projectPath) === projectPathKey(projectPath);
  const sessionMatches = !sessionId || current.sessionId === sessionId;
  if (projectMatches && sessionMatches) {
    window.localStorage.removeItem(storageKey);
  }
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

/**
 * Archived sessions use a single desktop key holding a projectPath → sessionIds
 * map. Remote projects are naturally bucketed by their `remote://<deviceId>/<projectId>`
 * path keys, which can never collide with desktop filesystem paths; the key
 * name and value shape stay unchanged for desktop compatibility.
 */
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

/**
 * Remote projects bucket their prompt history per device/project under
 * `somnia.remote.prompt-history:<deviceId>:<projectId>`; desktop projects
 * keep the unchanged global desktop key.
 */
function promptHistoryStorageKey(projectPath?: string | null): string {
  return remoteScopedStorageKey("prompt-history", projectPath) ?? PROMPT_HISTORY_STORAGE_KEY;
}

function readStoredPromptHistory(projectPath?: string | null): string[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const value = JSON.parse(window.localStorage.getItem(promptHistoryStorageKey(projectPath)) ?? "[]") as unknown;
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
  } catch {
    return [];
  }
}

function persistPromptHistory(history: string[], projectPath?: string | null) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(promptHistoryStorageKey(projectPath), JSON.stringify(history));
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
  if (normalized === "/model" || normalized === "/vision" || normalized === "/reasoning") {
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

async function buildPromptPayload(client: SomniaClient, prompt: string, images: PendingImage[]): Promise<PreparedPromptPayload> {
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

function renderWorkerLogContent(content: unknown): string {
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    const parts = content
      .map((item) => {
        if (typeof item === "string") {
          return item;
        }
        if (!isRecord(item)) {
          return "";
        }
        if (typeof item.text === "string") {
          return item.text;
        }
        if (typeof item.content === "string") {
          return item.content;
        }
        if (item.type === "tool_use" || item.type === "tool_call") {
          return "";
        }
        return "";
      })
      .filter((part) => part.trim());
    if (parts.length > 0) {
      return parts.join("\n\n");
    }
  }
  return stringifyToolValue(content ?? "");
}

function formatWorkerLogTimestamp(timestamp: unknown): string {
  if (typeof timestamp === "number" && Number.isFinite(timestamp)) {
    return formatRelativeTime(timestamp);
  }
  if (typeof timestamp === "string" && timestamp.trim()) {
    const parsed = Date.parse(timestamp);
    if (Number.isFinite(parsed)) {
      return formatRelativeTime(parsed / 1000);
    }
    return timestamp.trim();
  }
  return "";
}

function workerLogEventLabel(entry: TeamLogEntry): string {
  const eventType = String(entry.type ?? "event");
  if (eventType === "session_started") {
    const role = typeof entry.role === "string" && entry.role.trim() ? entry.role.trim() : "teammate";
    return `session started / ${role}`;
  }
  if (eventType === "session_resumed") {
    return "session resumed";
  }
  if (eventType === "tool_result_message") {
    return "tool results";
  }
  return eventType.replace(/_/g, " ");
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

function runtimeToolCallId(event: SidecarEvent): string {
  return readEventString(event.payload.tool_call_id, readEventString(event.turn_id, `tool-${event.timestamp ?? Date.now()}`));
}

function findLastRunningToolIndex(items: ConversationRuntimeItem[], toolName: string, toolCallId?: string): number {
  if (toolCallId) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item.type === "tool_call" && item.toolCall.id === toolCallId && item.toolCall.status === "running") {
        return index;
      }
    }
  }
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.type === "tool_call" && item.toolCall.name === toolName && item.toolCall.status === "running") {
      return index;
    }
  }
  return -1;
}

function readContextUsageFromPayload(value: unknown): ContextWindowUsage | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const payload = value as Partial<ContextWindowUsage>;
  const usedTokens = Number(payload.used_tokens);
  if (!Number.isFinite(usedTokens)) {
    return null;
  }
  const maxTokens = payload.max_tokens === null || payload.max_tokens === undefined ? null : Number(payload.max_tokens);
  const usagePercent = payload.usage_percent === null || payload.usage_percent === undefined ? null : Number(payload.usage_percent);
  return {
    used_tokens: Math.max(0, usedTokens),
    max_tokens: maxTokens !== null && Number.isFinite(maxTokens) ? Math.max(0, maxTokens) : null,
    usage_percent: usagePercent !== null && Number.isFinite(usagePercent) ? usagePercent : null,
    counter_name: typeof payload.counter_name === "string" ? payload.counter_name : "estimate",
  };
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

function buildCacheUsageSummary(tokenUsage: Record<string, number> | undefined | null): {
  ratio: number;
  inputTokens: number;
  cacheReadTokens: number;
  promptTokens: number;
} | null {
  if (!tokenUsage || typeof tokenUsage !== "object") {
    return null;
  }
  const inputTokens = Math.max(0, Number(tokenUsage.input_tokens) || 0);
  const cacheReadTokens = Math.max(0, Number(tokenUsage.cache_read_input_tokens) || 0);
  const promptTokens = inputTokens + cacheReadTokens;
  if (promptTokens <= 0 || cacheReadTokens <= 0) {
    return null;
  }
  return {
    ratio: cacheReadTokens / promptTokens,
    inputTokens,
    cacheReadTokens,
    promptTokens,
  };
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) {
    return "--";
  }
  return `${(Math.max(0, Math.min(1, value)) * 100).toFixed(1)}%`;
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

function ProviderSetupForm({
  mode,
  presets,
  selectedPreset,
  scope,
  providerName,
  providerType,
  baseUrl,
  apiKey,
  modelsText,
  defaultModel,
  customDraft,
  onModeChange,
  onScopeChange,
  onPresetChange,
  onProviderNameChange,
  onProviderTypeChange,
  onBaseUrlChange,
  onApiKeyChange,
  onModelsTextChange,
  onDefaultModelChange,
  onCustomDraftChange,
}: {
  mode: "preset" | "custom";
  presets: ProviderPresetDescriptor[];
  selectedPreset: string;
  scope: SettingsConfigScopeKey;
  providerName: string;
  providerType: string;
  baseUrl: string;
  apiKey: string;
  modelsText: string;
  defaultModel: string;
  customDraft: string;
  onModeChange: (mode: "preset" | "custom") => void;
  onScopeChange: (scope: SettingsConfigScopeKey) => void;
  onPresetChange: (presetId: string) => void;
  onProviderNameChange: (value: string) => void;
  onProviderTypeChange: (value: string) => void;
  onBaseUrlChange: (value: string) => void;
  onApiKeyChange: (value: string) => void;
  onModelsTextChange: (value: string) => void;
  onDefaultModelChange: (value: string) => void;
  onCustomDraftChange: (value: string) => void;
}) {
  const { t } = useI18n();
  const models = modelsText
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean);
  const selected = presets.find((preset) => preset.id === selectedPreset) ?? null;

  return (
    <div className="provider-setup-form">
      <div className="provider-setup-toggle" role="tablist" aria-label={t("providerSetup.modeLabel")}>
        <button type="button" className={mode === "preset" ? "selected" : ""} onClick={() => onModeChange("preset")}>
          {t("providerSetup.modePreset")}
        </button>
        <button type="button" className={mode === "custom" ? "selected" : ""} onClick={() => onModeChange("custom")}>
          {t("providerSetup.modeCustom")}
        </button>
      </div>

      {mode === "preset" ? (
        <>
          <div className="provider-setup-grid">
            <label>
              <span>{t("providerSetup.scope")}</span>
              <select value={scope} onChange={(event) => onScopeChange(event.currentTarget.value as SettingsConfigScopeKey)}>
                <option value="user">{t("settings.config.user")}</option>
                <option value="project">{t("settings.config.project")}</option>
              </select>
            </label>
            <label>
              <span>{t("providerSetup.preset")}</span>
              <select value={selectedPreset} onChange={(event) => onPresetChange(event.currentTarget.value)}>
                {presets.map((preset) => (
                  <option key={preset.id} value={preset.id}>
                    {preset.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>{t("providerSetup.providerName")}</span>
              <input value={providerName} onChange={(event) => onProviderNameChange(event.currentTarget.value)} />
            </label>
            <label>
              <span>{t("providerSetup.compatibility")}</span>
              <select value={providerType} onChange={(event) => onProviderTypeChange(event.currentTarget.value)}>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
              </select>
            </label>
            <label className="wide">
              <span>{t("settings.providerProfiles.baseUrl")}</span>
              <input value={baseUrl} onChange={(event) => onBaseUrlChange(event.currentTarget.value)} />
            </label>
            <label className="wide">
              <span>{t("settings.providerProfiles.apiKey")}</span>
              <input type="password" value={apiKey} onChange={(event) => onApiKeyChange(event.currentTarget.value)} />
            </label>
            <label className="wide">
              <span>{t("settings.providerProfiles.models")}</span>
              <input value={modelsText} onChange={(event) => onModelsTextChange(event.currentTarget.value)} />
            </label>
            <label>
              <span>{t("providerSetup.defaultModel")}</span>
              <select value={defaultModel} onChange={(event) => onDefaultModelChange(event.currentTarget.value)}>
                {models.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
                {defaultModel && !models.includes(defaultModel) ? <option value={defaultModel}>{defaultModel}</option> : null}
              </select>
            </label>
          </div>
          {selected?.notes || selected?.api_key_url ? (
            <p className="provider-setup-hint">
              {selected.notes}
              {selected.api_key_url ? (
                <>
                  {" "}
                  <a href={selected.api_key_url} target="_blank" rel="noreferrer">
                    {t("providerSetup.apiKeys")}
                  </a>
                </>
              ) : null}
            </p>
          ) : null}
        </>
      ) : (
        <ProviderProfilesEditor
          text={customDraft}
          inheritedText=""
          scope={scope}
          onChange={onCustomDraftChange}
          onDebugModel={async () => ({ ok: false, message: t("providerSetup.saveBeforeTest") })}
        />
      )}
    </div>
  );
}

function tomlString(value: string): string {
  return `"${String(value ?? "").replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function normalizeProviderNameForToml(value: string): string {
  return String(value || "provider")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9_-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "") || "provider";
}

export default App;
