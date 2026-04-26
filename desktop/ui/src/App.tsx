import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { chooseProjectFolder, ensureManagedSidecar, openWorkspaceRoot } from "./lib/desktop";
import { buildConversationRows, buildSessionPreview, formatRelativeTime, formatTodoLabel, sortSessions } from "./lib/messages";
import { SidecarClient, normalizeBaseUrl } from "./lib/sidecar";
import type {
  AgentSession,
  InteractionRequestState,
  ManagedSidecarConnection,
  ModelDescriptor,
  ProviderDescriptor,
  SidecarEvent,
  SidecarStatus,
  ToolLogDetail,
  ToolLogIndexEntry,
} from "./types";

const STORAGE_KEY = "somnia.desktop.sidecar-url";
const PROJECTS_STORAGE_KEY = "somnia.desktop.project-paths";
const DEFAULT_SIDECAR_URL = "http://127.0.0.1:8765";
const TOOL_LIMIT = 24;
const REASONING_LEVEL_OPTIONS = ["auto", "low", "medium", "high", "deep"] as const;

type ReasoningLevelOption = (typeof REASONING_LEVEL_OPTIONS)[number];
type ProjectState = {
  path: string;
  label: string;
  connection: ManagedSidecarConnection;
  status: SidecarStatus;
  sessions: AgentSession[];
  pendingInteractions: InteractionRequestState[];
  toolLogs: ToolLogIndexEntry[];
};

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
  const [streamingText, setStreamingText] = useState("");
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [pendingInteractions, setPendingInteractions] = useState<InteractionRequestState[]>([]);
  const [providers, setProviders] = useState<ProviderDescriptor[]>([]);
  const [models, setModels] = useState<ModelDescriptor[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [selectedReasoningLevel, setSelectedReasoningLevel] = useState<ReasoningLevelOption>("auto");
  const [toolLogs, setToolLogs] = useState<ToolLogIndexEntry[]>([]);
  const [activeToolLog, setActiveToolLog] = useState<ToolLogDetail | null>(null);
  const [sidebarSection, setSidebarSection] = useState<"sessions">("sessions");
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [projectMenuOpenKey, setProjectMenuOpenKey] = useState<string | null>(null);
  const [contextPanelOpen, setContextPanelOpen] = useState(true);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [bannerMessage, setBannerMessage] = useState("Point the UI at a running sidecar and start a session.");
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const clientRef = useRef<SidecarClient | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const projectClientsRef = useRef<Record<string, SidecarClient>>({});
  const projectSocketsRef = useRef<Record<string, WebSocket>>({});
  const selectedProjectPathRef = useRef<string | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const currentSessionRef = useRef<AgentSession | null>(null);
  const modelPickerRef = useRef<HTMLDivElement | null>(null);
  const projectMenuRef = useRef<HTMLDivElement | null>(null);
  const composerTextareaRef = useRef<HTMLTextAreaElement | null>(null);

  selectedSessionIdRef.current = selectedSessionId;
  selectedProjectPathRef.current = selectedProjectPath;
  currentSessionRef.current = currentSession;

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
      setStreamingText("");
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
        setStreamingText("");
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
      if (isActiveProject && event.session_id && event.session_id === selectedSessionIdRef.current) {
        setStreamingText("");
      }
      if (isActiveProject) {
        setActiveTurnId(event.turn_id ?? null);
      }
      return;
    }
    if (event.type === "assistant_delta") {
      if (!isActiveProject || !selectedSessionIdRef.current || event.session_id !== selectedSessionIdRef.current) {
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
    if (event.type === "tool_finished") {
      if (isActiveProject) {
        void refreshToolLogs();
      }
      return;
    }
    if (event.type === "provider_switched" || event.type === "reasoning_level_updated") {
      if (isActiveProject) {
        void refreshStatusAndProviders();
      }
      return;
    }
    if (event.type === "authorization_requested" || event.type === "mode_switch_requested") {
      if (isActiveProject) {
        setContextPanelOpen(true);
        void refreshInteractions();
      }
      return;
    }
    if (event.type === "interrupt_completed" || event.type === "error") {
      if (isActiveProject) {
        setActiveTurnId((current) => (current === event.turn_id ? null : current));
        setStreamingText("");
        void refreshInteractions();
        void refreshStatusAndProviders();
      }
      return;
    }
    if (event.type === "turn_result") {
      if (isActiveProject) {
        setActiveTurnId((current) => (current === event.turn_id ? null : current));
        setStreamingText("");
      }
      const payloadSession = readSessionFromPayload(event.payload.session);
      if (payloadSession) {
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
    setSidebarSection("sessions");
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
    setStreamingText("");
    if (knownSessions) {
      setSessions(sortSessions(knownSessions.map((session) => (session.id === loadedSession.id ? loadedSession : session))));
    } else {
      upsertProjectSession(projectPath, loadedSession);
    }
  }

  function upsertSession(session: AgentSession) {
    upsertProjectSession(selectedProjectPathRef.current, session);
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

  const conversationRows = buildConversationRows(currentSession, streamingText);
  const firstPendingInteraction = pendingInteractions[0] ?? null;
  const activeProviderLabel = status?.provider ?? selectedProvider ?? "Provider";
  const activeModelLabel = status?.model ?? selectedModel ?? "Model";
  const activeReasoningLabel = formatReasoningLevel(status?.reasoning_level ?? selectedReasoningLevel);
  const conversationPreview = currentSession ? buildSessionPreview(currentSession) : "";
  const conversationTitle = truncateTopic(conversationPreview || selectedSessionId || "New conversation");
  const workspaceRootPath = status?.workspace_root ?? "";
  const workspaceRootName = workspaceRootPath ? getPathLeafName(workspaceRootPath) : "workspace";
  const sessionProjectGroups = projects.map((project) => ({
    key: project.path,
    label: project.label,
    path: project.path,
    sessions: project.sessions,
  }));
  const visibleProjectCount = sessionProjectGroups.length;
  return (
    <div className="shell">
      <div className="ambient ambient-left" />
      <div className="ambient ambient-right" />
      <main className={`workspace ${contextPanelOpen ? "context-open" : "context-collapsed"}`}>
        <aside className="panel sidebar-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Projects</p>
              <h2>Projects</h2>
            </div>
            <div className="panel-header-actions">
              <span className="panel-count">{visibleProjectCount} total</span>
              <button className="action primary sidebar-new" onClick={() => void handleCreateProject()} disabled={busyAction !== null}>
                New Project
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
                            <span className="project-toggle-caret">{isCollapsed ? ">" : "v"}</span>
                            <strong>{group.label}</strong>
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
                            ...
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
                                New Session
                              </button>
                            </div>
                          ) : null}
                        </div>
                      </div>
                      {isCollapsed ? null : (
                        <div className="project-session-list">
                          {group.sessions.map((session) => (
                            <button
                              key={session.id}
                              className={`session-card ${selectedProjectPath === group.path && selectedSessionId === session.id ? "selected" : ""}`}
                              onClick={() => {
                                setContextPanelOpen(true);
                                void activateProject(group.path, projectClientsRef.current[group.path]).then(() =>
                                  selectSession(session.id, projectClientsRef.current[group.path], group.sessions, group.path),
                                );
                              }}
                            >
                              <div className="session-card-head">
                                <strong>{session.id}</strong>
                                <span>{formatRelativeTime(session.updated_at ?? session.created_at)}</span>
                              </div>
                              <p>{buildSessionPreview(session)}</p>
                              <div className="session-card-meta">
                                <span>{session.messages.length} msgs</span>
                              </div>
                            </button>
                          ))}
                        </div>
                      )}
                    </section>
                  );
                })}
              </div>
            )}
          </div>
        </aside>

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
                ...
              </button>
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
              ref={composerTextareaRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Ask Somnia to inspect, plan, or implement against the current workspace."
              rows={1}
            />
            <div className="composer-actions">
              <div className="composer-meta">
                <div className="composer-controls">
                  <span className="mode-pill">{status?.execution_mode_title ?? "Execution mode unavailable"}</span>
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
                </div>
              </div>
              <div className="composer-cta">
                <button className="action primary" onClick={() => void handleSendPrompt()} disabled={!draft.trim() || busyAction !== null}>
                  Send
                </button>
                {activeTurnId ? (
                  <button className="action danger" onClick={() => void handleInterrupt()} disabled={busyAction !== null}>
                    Interrupt
                  </button>
                ) : null}
              </div>
            </div>
          </div>
        </section>

        {contextPanelOpen ? (
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
        ) : null}
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

function upsertProject(projects: ProjectState[], nextProject: ProjectState): ProjectState[] {
  const others = projects.filter((project) => project.path !== nextProject.path);
  return [...others, nextProject].sort((left, right) => left.label.localeCompare(right.label));
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

function normalizeReasoningLevel(value: string | null | undefined): ReasoningLevelOption {
  const normalized = String(value ?? "")
    .trim()
    .toLowerCase();
  return (REASONING_LEVEL_OPTIONS as readonly string[]).includes(normalized) ? (normalized as ReasoningLevelOption) : "auto";
}

function formatReasoningLevel(value: string | null | undefined): string {
  const normalized = normalizeReasoningLevel(value);
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
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
