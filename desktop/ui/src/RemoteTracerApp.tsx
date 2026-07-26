import { useEffect, useRef, useState } from "react";
import type { ClipboardEvent as ReactClipboardEvent, KeyboardEvent as ReactKeyboardEvent } from "react";

import type { AgentSession, ConversationRuntimeItem, TaskGraphItem, TeamMemberActivity, ToolLogIndexEntry, WorkspacePathSuggestion } from "./types";
import { createConversationState, transitionConversationEvent, type ConversationState } from "./lib/conversation-state";
import { buildConversationRows, stringifyToolValue } from "./lib/messages";
import { RemoteSomniaConnection } from "./lib/remote-somnia-connection";
import type { SomniaConnectionNotification } from "./lib/somnia-connection";
import { useRemoteAccess } from "./lib/use-remote-access";
import { RemoteConversationRow } from "./RemoteRichContent";
import { deriveRemoteConnectionState, remoteConnectionCopy } from "./lib/remote-connection-state";
import ConversationComposer from "./components/ConversationComposer";
import ConversationWorkspace from "./components/ConversationWorkspace";
import ConversationPanel from "./components/ConversationPanel";
import SessionSidebar from "./components/SessionSidebar";
import ProgressPanel from "./components/ProgressPanel";
import ContextPanel from "./components/ContextPanel";

const defaults = readConnectionDefaults();

type RemoteQueuedPrompt = {
  id: string;
  prompt: string;
  images: RemotePendingImage[];
  injectionRequested: boolean;
};

const REMOTE_COMMANDS = ["/init", "/scan", "/symbols", "/image", "/paste-image", "/model", "/vision", "/reasoning", "/providers", "/hooks", "/undo", "/checkpoint", "/rollback", "/compact", "/janitor", "/skills", "/tasks", "/team", "/mcp", "/bg", "/help", "/exit"];
type RemotePendingImage = { id: string; name: string; mediaType: string; dataUrl: string };

export default function RemoteTracerApp() {
  const access = useRemoteAccess(defaults.relayUrl);
  const [projectId, setProjectId] = useState(defaults.projectId);
  const [connectionState, setConnectionState] = useState("disconnected");
  const [session, setSession] = useState<AgentSession | null>(null);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [archivedSessionIds, setArchivedSessionIds] = useState<Set<string>>(() => new Set());
  const [draft, setDraft] = useState("");
  const [pendingImages, setPendingImages] = useState<RemotePendingImage[]>([]);
  const [commandPickerOpen, setCommandPickerOpen] = useState(false);
  const [pathPickerOpen, setPathPickerOpen] = useState(false);
  const [pathSuggestions, setPathSuggestions] = useState<WorkspacePathSuggestion[]>([]);
  const [selectedSuggestionIndex, setSelectedSuggestionIndex] = useState(0);
  const [history, setHistory] = useState<string[]>([]);
  const [historyCursor, setHistoryCursor] = useState<number | null>(null);
  const [composerCursor, setComposerCursor] = useState(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [pendingPrompt, setPendingPrompt] = useState("");
  const [queuedPrompts, setQueuedPrompts] = useState<RemoteQueuedPrompt[]>([]);
  const [progress, setProgress] = useState<ConversationState | null>(null);
  const [teamMembers, setTeamMembers] = useState<TeamMemberActivity[]>([]);
  const [tasks, setTasks] = useState<TaskGraphItem[]>([]);
  const [toolLogs, setToolLogs] = useState<ToolLogIndexEntry[]>([]);
  const [diagnosticDetail, setDiagnosticDetail] = useState("");
  const [conversationBusy, setConversationBusy] = useState(false);
  const connectionRef = useRef<RemoteSomniaConnection | null>(null);
  const conversationRef = useRef<ConversationState | null>(null);
  const autoConnectTargetRef = useRef("");

  const busy = access.busy || conversationBusy;

  useEffect(() => {
    setHistory(readRemotePromptHistory(access.deviceId, projectId));
  }, [access.deviceId, projectId]);

  useEffect(() => {
    const currentDevice = access.devices.find((device) => device.device_id === access.deviceId);
    if (!access.deviceId || !currentDevice) return;
    const available = currentDevice.projects;
    if (available.length > 0 && !available.some((project) => project.project_id === projectId)) {
      setProjectId(available[0].project_id);
    }
  }, [access.deviceId, access.devices, projectId]);

  useEffect(() => {
    if (!access.pairingCode || !access.pairingExpiresAt) return;
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      if (Date.now() / 1000 >= access.pairingExpiresAt!) {
        window.clearInterval(timer);
        return;
      }
      void access.refreshDevices().then((devices) => {
        // Do not stop polling while the newly paired Device is still offline; an unrelated
        // online Device must never trigger an automatic connection.
        const newlyPaired = devices.find((device) => device.name === access.pairingName && device.status === "online");
        if (newlyPaired && Date.now() - startedAt > 500) {
          access.setDeviceId(newlyPaired.device_id);
          access.setNotice(`Device ${newlyPaired.name} is online and ready to connect.`);
          window.clearInterval(timer);
        }
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [access.pairingCode, access.pairingExpiresAt]);

  useEffect(() => {
    if (!access.authenticated) return;
    const timer = window.setInterval(() => {
      void access.refreshDevices().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [access.authenticated]);

  useEffect(() => {
    setDraft(readRemoteDraft(access.deviceId, projectId, session?.id ?? ""));
    setHistoryCursor(null);
    setPendingImages([]);
  }, [access.deviceId, projectId, session?.id]);

  useEffect(() => {
    writeRemoteDraft(access.deviceId, projectId, session?.id ?? "", draft);
  }, [access.deviceId, projectId, session?.id, draft]);

  useEffect(() => {
    const trimmed = draft.trimStart();
    const commandOpen = /^\/[^\s]*$/.test(trimmed);
    setCommandPickerOpen(commandOpen);
    if (!commandOpen) setSelectedSuggestionIndex(0);
  }, [draft]);

  useEffect(() => {
    const mention = currentRemotePathMention(draft, composerCursor);
    const connection = connectionRef.current;
    if (!mention || !connection || connectionState !== "connected") {
      setPathPickerOpen(false);
      setPathSuggestions([]);
      return;
    }
    let cancelled = false;
    void connection.listWorkspacePaths(mention.query, 30).then((paths) => {
      if (cancelled) return;
      setPathSuggestions(paths);
      setPathPickerOpen(paths.length > 0);
      setCommandPickerOpen(false);
      setSelectedSuggestionIndex(0);
    }).catch(() => { if (!cancelled) setPathPickerOpen(false); });
    return () => { cancelled = true; };
  }, [draft, composerCursor, connectionState]);

  async function revokeSelectedDevice() {
    if (await access.revokeSelectedDevice()) {
      connectionRef.current?.close();
      connectionRef.current = null;
      conversationRef.current = null;
      setSession(null);
      setSessions([]);
      setQueuedPrompts([]);
      setProgress(null);
      clearExecutionDetails();
      setConnectionState("disconnected");
    }
  }

  async function signOut() {
    await access.signOut();
    connectionRef.current?.close();
    connectionRef.current = null;
    conversationRef.current = null;
    setSession(null);
    setQueuedPrompts([]);
    setProgress(null);
    clearExecutionDetails();
    setConnectionState("disconnected");
  }

  async function connect() {
    if (!access.deviceId) return;
    setConversationBusy(true);
    try {
      if (!await access.verifyAccess()) return;
      connectionRef.current?.close();
      setSession(null);
      setSessions([]);
      setPendingPrompt("");
      setQueuedPrompts([]);
      setProgress(null);
      clearExecutionDetails();
      conversationRef.current = null;
      const connection = new RemoteSomniaConnection({
        relayUrl: access.relayUrl,
        deviceId: access.deviceId,
        projectId,
      });
      connectionRef.current = connection;
      connection.subscribe(handleConnectionNotification);
    } catch (error) {
      access.setNotice(formatError(error));
    } finally {
      setConversationBusy(false);
    }
  }

  function handleConnectionNotification(notification: SomniaConnectionNotification) {
    if (notification.kind === "state") {
      setConnectionState(notification.state);
      if (notification.state === "disconnected" || notification.state === "error") {
        autoConnectTargetRef.current = "";
      }
      access.setNotice(notification.error ?? connectionStateMessage(notification.state));
      if (notification.state === "connected") {
        void connectionRef.current?.query({ type: "session.list" }).then((loaded) => {
          setSessions(loaded);
          const savedSessionId = readRemoteLastSession(access.deviceId, projectId);
          const saved = loaded.find((candidate) => candidate.id === savedSessionId);
          if (saved) void selectSession(saved.id);
        }).catch((error) => access.setNotice(formatError(error)));
      }
      return;
    }
    if (notification.kind === "protocol_error") {
      access.setNotice(notification.error);
      return;
    }
    if (notification.kind === "snapshot") {
      const sessions = notification.snapshot.sessions;
      const runtime = notification.snapshot.runtime;
      const runtimeStatus = runtime && typeof runtime === "object"
        ? String((runtime as { status?: unknown }).status ?? "unknown")
        : "unknown";
      const activeTurns = runtime && typeof runtime === "object" && Array.isArray((runtime as { active_turns?: unknown }).active_turns)
        ? (runtime as { active_turns: Array<{ session_id?: unknown; turn_id?: unknown }> }).active_turns
        : [];
      const currentSessionId = conversationRef.current?.session?.id ?? session?.id;
      if (Array.isArray(sessions) && currentSessionId) {
        const knownSession = sessions.find((candidate) => (
          candidate && typeof candidate === "object" && (candidate as { id?: unknown }).id === currentSessionId
        ));
        if (knownSession && connectionRef.current) {
          void connectionRef.current.query({ type: "session.load", sessionId: currentSessionId }).then((recovered) => {
            setSession(recovered);
            const recoveredState = createConversationState(recovered);
            const activeTurn = activeTurns.find((candidate) => candidate.session_id === recovered.id && typeof candidate.turn_id === "string");
            if (activeTurn) {
              recoveredState.activeTurnId = activeTurn.turn_id as string;
            }
            conversationRef.current = recoveredState;
            setProgress(recoveredState);
            void refreshExecutionDetails(recovered.id);
          }).catch((error) => access.setNotice(formatError(error)));
        }
      }
      access.setNotice(`Remote stream resynchronized from the Device (Runtime ${runtimeStatus}).`);
      return;
    }
    const event = notification.event;
    if (!event.session_id) return;
    const previous = conversationRef.current ?? createConversationState(event.session_id);
    const transition = transitionConversationEvent(previous, event);
    conversationRef.current = transition.state;
    setProgress(transition.state);
    if (event.type === "loop_user_message_injected") {
      const injectionId = typeof event.payload.injection_id === "string" ? event.payload.injection_id : "";
      if (injectionId) {
        setQueuedPrompts((current) => current.filter((prompt) => prompt.id !== injectionId));
      }
    }
    if (transition.effect.type === "turn_started") {
    } else if (transition.effect.type === "turn_completed") {
      if (transition.state.session) setSession(transition.state.session);
      setPendingPrompt("");
      setConversationBusy(false);
      void refreshExecutionDetails(event.session_id);
      void startNextQueuedPrompt(event.session_id, transition.state.session);
    } else if (event.type === "tool_finished" || event.type === "subagent_activity") {
      void refreshExecutionDetails(event.session_id);
    }
  }

  async function createSession() {
    const connection = connectionRef.current;
    if (!connection) return;
    setConversationBusy(true);
    try {
      const created = await connection.execute({ type: "session.create" });
      conversationRef.current = createConversationState(created);
      setProgress(conversationRef.current);
      setSession(created);
      rememberRemoteLastSession(access.deviceId, projectId, created.id);
      setSessions((current) => [created, ...current]);
      access.setNotice(`Session ${created.id} is ready.`);
    } catch (error) {
      access.setNotice(formatError(error));
    } finally {
      setConversationBusy(false);
    }
  }

  async function selectSession(sessionId: string) {
    const connection = connectionRef.current;
    if (!connection) return;
    try {
      const loaded = await connection.query({ type: "session.load", sessionId });
      setSession(loaded);
      rememberRemoteLastSession(access.deviceId, projectId, loaded.id);
      conversationRef.current = createConversationState(loaded);
      setProgress(conversationRef.current);
      setQueuedPrompts([]);
      clearExecutionDetails();
      await refreshExecutionDetails(sessionId);
    } catch (error) {
      access.setNotice(formatError(error));
    }
  }

  async function deleteSession(sessionId: string) {
    const connection = connectionRef.current;
    if (!connection) return;
    try {
      const result = await connection.execute({ type: "session.delete", sessionId });
      if (!result.deleted) throw new Error("Session was already deleted.");
      setSessions((current) => current.filter((candidate) => candidate.id !== sessionId));
      setArchivedSessionIds((current) => { const next = new Set(current); next.delete(sessionId); return next; });
      if (session?.id === sessionId) {
        setSession(null);
        setProgress(null);
        setQueuedPrompts([]);
        clearExecutionDetails();
        conversationRef.current = null;
      }
    } catch (error) { access.setNotice(formatError(error)); }
  }

  async function sendPrompt() {
    const connection = connectionRef.current;
    const prompt = draft.trim();
    if (!connection || !session || (!prompt && pendingImages.length === 0)) return;
    const images = pendingImages;
    if (images.length === 0 && (prompt === "/compact" || prompt === "/janitor")) {
      setCommandPickerOpen(false);
      setConversationBusy(true);
      try {
        const result = prompt === "/compact" ? await connection.compactSession(session.id) : await connection.janitorSession(session.id);
        setSession(result.session);
        setSessions((current) => current.map((candidate) => candidate.id === result.session.id ? result.session : candidate));
        setDraft("");
        access.setNotice(result.message);
      } catch (error) { access.setNotice(formatError(error)); }
      finally { setConversationBusy(false); }
      rememberRemotePrompt(access.deviceId, projectId, prompt, setHistory);
      return;
    }
    if (progress?.activeTurnId) {
      setQueuedPrompts((current) => [...current, { id: createRequestId("queue"), prompt, images, injectionRequested: false }]);
      setDraft("");
      setPendingImages([]);
      rememberRemotePrompt(access.deviceId, projectId, prompt, setHistory);
      access.setNotice("Prompt queued for this active Session.");
      return;
    }
    if (busy) return;
    setConversationBusy(true);
    setPendingPrompt(prompt);
    setProgress(conversationRef.current);
    try {
      await connection.execute({ type: "turn.start", sessionId: session.id, userInput: await buildRemotePromptPayload(connection, prompt, images) });
      setDraft("");
      setPendingImages([]);
      rememberRemotePrompt(access.deviceId, projectId, prompt, setHistory);
    } catch (error) {
      setConversationBusy(false);
      setPendingPrompt("");
      access.setNotice(formatError(error));
    }
  }

  function chooseCommand(command: string) {
    setDraft(`${command} `);
    setCommandPickerOpen(false);
    setPathPickerOpen(false);
  }

  function choosePath(path: string) {
    const mention = currentRemotePathMention(draft, composerCursor);
    if (!mention) return;
    const next = `${draft.slice(0, mention.queryStart)}${path}${draft.slice(mention.end)}`;
    setDraft(next);
    setComposerCursor(mention.queryStart + path.length);
    setPathPickerOpen(false);
  }

  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    const suggestions = pathPickerOpen ? pathSuggestions.map((item) => item.path) : REMOTE_COMMANDS.filter((item) => item.startsWith(draft.trimStart()));
    if ((pathPickerOpen || commandPickerOpen) && suggestions.length > 0) {
      if (event.key === "ArrowDown") { event.preventDefault(); setSelectedSuggestionIndex((current) => (current + 1) % suggestions.length); return; }
      if (event.key === "ArrowUp") { event.preventDefault(); setSelectedSuggestionIndex((current) => (current - 1 + suggestions.length) % suggestions.length); return; }
      if (event.key === "Escape") { event.preventDefault(); setCommandPickerOpen(false); setPathPickerOpen(false); return; }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        if (pathPickerOpen) choosePath(suggestions[selectedSuggestionIndex]);
        else chooseCommand(suggestions[selectedSuggestionIndex]);
        return;
      }
    }
    if (event.key === "ArrowUp" && !draft && history.length > 0) {
      event.preventDefault();
      const next = historyCursor === null ? history.length - 1 : Math.max(0, historyCursor - 1);
      setHistoryCursor(next); setDraft(history[next]); return;
    }
    if (event.key === "ArrowDown" && historyCursor !== null) {
      event.preventDefault();
      const next = historyCursor + 1;
      if (next >= history.length) { setHistoryCursor(null); setDraft(""); } else { setHistoryCursor(next); setDraft(history[next]); }
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendPrompt(); }
  }

  async function handleComposerPaste(event: ReactClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (files.length === 0) return;
    event.preventDefault();
    const images = await Promise.all(files.map(readRemoteImage));
    setPendingImages((current) => [...current, ...images].slice(-8));
    if (!draft.trim()) setDraft("Look at this image.");
  }

  async function handleImageFiles(files: FileList | null) {
    const selected = Array.from(files ?? []).filter((file) => file.type.startsWith("image/"));
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (selected.length === 0) return;
    const images = await Promise.all(selected.map(readRemoteImage));
    setPendingImages((current) => [...current, ...images].slice(-8));
    if (!draft.trim()) setDraft("Look at this image.");
  }

  async function startNextQueuedPrompt(sessionId: string, completedSession: AgentSession | null) {
    const next = queuedPrompts[0];
    const connection = connectionRef.current;
    if (!next || !connection || session?.id !== sessionId) return;
    setQueuedPrompts((current) => current.slice(1));
    setConversationBusy(true);
    setPendingPrompt(next.prompt);
    try {
      await connection.execute({
        type: "turn.start",
        sessionId,
        userInput: await buildRemotePromptPayload(connection, next.prompt, next.images),
      });
      if (completedSession) setSession(completedSession);
      access.setNotice("Queued prompt started.");
    } catch (error) {
      setQueuedPrompts((current) => [next, ...current]);
      setPendingPrompt("");
      setConversationBusy(false);
      access.setNotice(formatError(error));
    }
  }

  async function injectQueuedPrompt(prompt: RemoteQueuedPrompt) {
    const connection = connectionRef.current;
    const turnId = progress?.activeTurnId;
    if (!connection || !turnId || prompt.injectionRequested) return;
    setQueuedPrompts((current) => current.map((candidate) => candidate.id === prompt.id ? { ...candidate, injectionRequested: true } : candidate));
    try {
      const result = await connection.queueLoopInjection(turnId, prompt.id, await buildRemotePromptPayload(connection, prompt.prompt, prompt.images));
      if (!result.queued) throw new Error("The active Turn rejected this injection. It may have finished already.");
      access.setNotice("Queued prompt will be injected on the next agent loop.");
    } catch (error) {
      setQueuedPrompts((current) => current.map((candidate) => candidate.id === prompt.id ? { ...candidate, injectionRequested: false } : candidate));
      access.setNotice(formatError(error));
    }
  }

  async function interruptActiveTurn() {
    const connection = connectionRef.current;
    const turnId = progress?.activeTurnId;
    if (!connection || !turnId) return;
    try {
      const result = await connection.interruptTurn(turnId);
      access.setNotice(result.interrupted ? "Interrupt requested." : "The Turn had already finished.");
    } catch (error) {
      access.setNotice(formatError(error));
    }
  }

  const connected = connectionState === "connected";
  const selectedDevice = access.devices.find((device) => device.device_id === access.deviceId);
  const projects = selectedDevice?.projects ?? [];

  useEffect(() => {
    if (!access.authenticated || selectedDevice?.status !== "online" || !projectId || !["disconnected", "error"].includes(connectionState) || busy) return;
    const target = `${access.deviceId}:${projectId}`;
    if (autoConnectTargetRef.current === target) return;
    autoConnectTargetRef.current = target;
    void connect().catch(() => { autoConnectTargetRef.current = ""; });
  }, [access.authenticated, access.deviceId, access.pairingCode, selectedDevice?.status, projectId, connectionState, busy]);

  useEffect(() => {
    if (projects.length > 0 && !projects.some((project) => project.project_id === projectId)) {
      setProjectId(projects[0].project_id);
    }
  }, [projectId, projects]);

  useEffect(() => {
    const raw = localStorage.getItem(archiveStorageKey(access.deviceId, projectId));
    try {
      setArchivedSessionIds(new Set(JSON.parse(raw ?? "[]") as string[]));
    } catch {
      setArchivedSessionIds(new Set());
    }
  }, [access.deviceId, projectId]);

  useEffect(() => {
    const sessionId = session?.id;
    if (!connected || !sessionId || !progress?.activeTurnId) return;
    const interval = window.setInterval(() => void refreshExecutionDetails(sessionId), 1500);
    return () => window.clearInterval(interval);
    // Refresh the Device-owned team/task state while a Turn is active.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, progress?.activeTurnId, session?.id]);

  function setArchived(sessionId: string) {
    setArchivedSessionIds((current) => {
      const next = new Set(current).add(sessionId);
      localStorage.setItem(archiveStorageKey(access.deviceId, projectId), JSON.stringify([...next]));
      return next;
    });
  }

  function restoreArchived() {
    localStorage.removeItem(archiveStorageKey(access.deviceId, projectId));
    setArchivedSessionIds(new Set());
  }

  function switchTarget(deviceId: string, nextProjectId: string) {
    connectionRef.current?.close();
    connectionRef.current = null;
    conversationRef.current = null;
    setSession(null);
    setPendingPrompt("");
    setQueuedPrompts([]);
    setProgress(null);
    clearExecutionDetails();
    setConnectionState("disconnected");
    access.setDeviceId(deviceId);
    setProjectId(nextProjectId);
    localStorage.setItem("somnia.remote.last-target", JSON.stringify({ deviceId, projectId: nextProjectId }));
  }

  function clearExecutionDetails() {
    setTeamMembers([]);
    setTasks([]);
    setToolLogs([]);
    setDiagnosticDetail("");
  }

  async function refreshExecutionDetails(sessionId: string) {
    const connection = connectionRef.current;
    if (!connection) return;
    try {
      const [members, nextTasks, logs] = await Promise.all([
        connection.listTeamMembers(sessionId), connection.listTasks(sessionId), connection.listToolLogs(24),
      ]);
      if (conversationRef.current?.sessionId !== sessionId) return;
      setTeamMembers(members);
      setTasks(nextTasks);
      setToolLogs(logs);
    } catch (error) {
      access.setNotice(`Execution details unavailable: ${formatError(error)}`);
    }
  }

  async function showToolLog(logId: string) {
    const connection = connectionRef.current;
    if (!connection) return;
    try { const detail = await connection.getToolLog(logId); setDiagnosticDetail(detail.rendered || stringifyToolValue(detail)); }
    catch (error) { access.setNotice(formatError(error)); }
  }

  async function showThinkingLog(path: string) {
    const connection = connectionRef.current;
    if (!connection) return;
    try { const detail = await connection.getThinkingLog(path); setDiagnosticDetail(detail.text); }
    catch (error) { access.setNotice(formatError(error)); }
  }

  async function showTeamLog(name: string) {
    const connection = connectionRef.current;
    if (!connection || !session) return;
    try { const detail = await connection.getTeamLog(name, session.id); setDiagnosticDetail(detail.rendered); }
    catch (error) { access.setNotice(formatError(error)); }
  }

  const runtimeItems: ConversationRuntimeItem[] = progress ? [
    ...(progress.thinking ? [{ id: `thinking-${progress.activeTurnId ?? "active"}`, type: "thinking_log" as const, thinkingLog: { turnId: progress.activeTurnId, path: progress.thinking.path, text: progress.thinking.text, status: progress.thinking.status }, isStreaming: progress.thinking.status === "running" }] : []),
    ...progress.tools.map((tool) => ({ id: `tool-${tool.id}`, type: "tool_call" as const, toolCall: { ...tool, input: stringifyToolValue(tool.input), output: stringifyToolValue(tool.output), contentBlocks: tool.contentBlocks as never[] } })),
    ...(progress.assistantText ? [{ id: `assistant-${progress.activeTurnId ?? "active"}`, type: "assistant_text" as const, text: progress.assistantText, isStreaming: true }] : []),
    ...progress.injectedUserMessages.map((message) => ({ id: `injected-${message.id}`, type: "user_text" as const, text: message.text })),
  ] : [];
  const conversationRows = buildConversationRows(session, runtimeItems, pendingPrompt ? { id: "remote-pending", sessionId: session?.id ?? null, userText: pendingPrompt, placeholderText: "Working…" } : null, progress?.activeTurnId ? session?.messages.length ?? 0 : null);
  if (!access.authenticated) {
    return (
      <main className="remote-shell remote-shell-login">
        <RemoteHeader state="unpaired" deviceStatus="offline" deviceId="" projectId={projectId} />
        <form className="remote-login" onSubmit={(event) => { event.preventDefault(); void access.signIn(); }}>
          <h1>Somnia Remote</h1>
          {!isSameOriginRelay(access.relayUrl) ? <label>Relay<input value={access.relayUrl} onChange={(event) => access.setRelayUrl(event.target.value)} /></label> : null}
          <label>Username<input value={access.username} onChange={(event) => access.setUsername(event.target.value)} autoComplete="username" /></label>
          <label>Password<input type="password" value={access.password} onChange={(event) => access.setPassword(event.target.value)} autoComplete="current-password" /></label>
          <button type="submit" disabled={busy || !access.username.trim() || !access.password}>Sign in</button>
          <div className="remote-notice" role="status">{access.notice}</div>
        </form>
      </main>
    );
  }

  return (
    <main className="remote-shell">
      <RemoteHeader
        state={deriveRemoteConnectionState({ transport: connectionState, deviceStatus: selectedDevice?.status, hasProject: Boolean(projectId) })}
        deviceStatus={selectedDevice?.status ?? "offline"}
        deviceId={access.deviceId}
        projectId={projectId}
      />
      <details className="remote-access-settings" open={!access.devices.length || Boolean(access.pairingCode)}>
        <summary>Device &amp; connection</summary>
        <section className="remote-connection" aria-label="Remote connection">
        <label>Device
          <select aria-label="Device" value={access.deviceId} onChange={(event) => {
            const device = access.devices.find((candidate) => candidate.device_id === event.target.value);
            switchTarget(event.target.value, device?.projects[0]?.project_id ?? "");
          }} disabled={busy || Boolean(progress?.activeTurnId)}>
            <option value="">Select Device</option>
            {access.devices.filter((device) => !device.revoked_at).map((device) => <option key={device.device_id} value={device.device_id}>{device.name} ({device.status})</option>)}
          </select>
        </label>
        <label>Project
          <select aria-label="Project" value={projectId} onChange={(event) => switchTarget(access.deviceId, event.target.value)} disabled={busy || Boolean(progress?.activeTurnId) || projects.length === 0}>
            <option value="">Select Project</option>
            {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
          </select>
        </label>
        <button type="button" onClick={() => void connect()} disabled={!access.deviceId || !projectId || selectedDevice?.status !== "online" || busy}>{connected ? "Reconnect" : "Connect"}</button>
        <button type="button" onClick={() => void revokeSelectedDevice()} disabled={!access.deviceId || busy}>Revoke selected Device</button>
        <button type="button" onClick={() => void signOut()} disabled={busy}>Sign out</button>
        </section>
        <section className="remote-pairing" aria-label="Device pairing">
        <label>New Device name<input value={access.pairingName} onChange={(event) => access.setPairingName(event.target.value)} /></label>
        <button type="button" onClick={() => void access.createPairing()} disabled={!access.pairingName.trim() || busy}>Create pairing code</button>
        {access.pairingCode ? <>
          <output className="remote-pairing-code" aria-label="Pairing code">{access.pairingCode}</output>
          <button type="button" onClick={() => void navigator.clipboard?.writeText(new URL(`somnia://pair?relay=${encodeURIComponent(access.relayUrl)}&code=${encodeURIComponent(access.pairingCode)}`).toString())}>Copy pairing link</button>
          <small>Run <code>somnia-connector setup --relay {access.relayUrl} --code {access.pairingCode}</code> on the computer. This code expires at {new Date(access.pairingExpiresAt! * 1000).toLocaleTimeString()}.</small>
        </> : null}
        </section>
      </details>
      <div className="remote-notice" role="status">{access.notice}</div>
      {queuedPrompts.length ? <section className="remote-queue" aria-label="Queued prompts">
        <strong>Queued prompts ({queuedPrompts.length})</strong>
        {queuedPrompts.map((prompt) => <div className="remote-queue-row" key={prompt.id}><span>{prompt.prompt}</span><button type="button" onClick={() => void injectQueuedPrompt(prompt)} disabled={!progress?.activeTurnId || prompt.injectionRequested}>{prompt.injectionRequested ? "Waiting for next loop" : "Inject next loop"}</button><button type="button" onClick={() => setQueuedPrompts((current) => current.filter((item) => item.id !== prompt.id))} aria-label={`Remove queued prompt ${prompt.id}`}>Remove</button></div>)}
      </section> : null}
      <ConversationWorkspace className="remote-workspace">
        <SessionSidebar className="remote-session-pane" ariaLabel="Session">
          <div className="remote-pane-heading"><span>Session</span><button type="button" onClick={() => void createSession()} disabled={!connected || busy}>New</button></div>
          {sessions.filter((candidate) => !archivedSessionIds.has(candidate.id)).map((candidate) => <div className={`remote-session-row ${session?.id === candidate.id ? "remote-session-row-selected" : ""}`} key={candidate.id}><button type="button" onClick={() => void selectSession(candidate.id)}><strong>{candidate.preview ?? candidate.id}</strong><span>{candidate.messages.length} messages</span></button><button type="button" onClick={() => setArchived(candidate.id)}>Archive</button><button type="button" onClick={() => void deleteSession(candidate.id)}>Delete</button></div>)}
          {Array.from(archivedSessionIds).length ? <button type="button" onClick={restoreArchived}>Restore archived</button> : null}
          {!sessions.length ? <p className="remote-empty">No active Session</p> : null}
        </SessionSidebar>
        <ConversationPanel className="remote-conversation" ariaLabel="Conversation">
          <div className="remote-messages">
            {conversationRows.map((row) => <RemoteConversationRow key={row.id} row={row} resolveImage={(path) => {
              const connection = connectionRef.current;
              return connection ? connection.getWorkspaceImage(path) : Promise.reject(new Error("Remote Device is offline."));
            }} />)}
            {!session ? <p className="remote-conversation-empty">Create a Session to begin.</p> : null}
          </div>
          {session ? <ProgressPanel className="remote-progress" ariaLabel="Execution progress">
            <details open><summary>Progress</summary>
              {progress?.thinking ? <button type="button" onClick={() => progress.thinking?.path && void showThinkingLog(progress.thinking.path)} disabled={!progress.thinking.path}>Thinking · {progress.thinking.status}</button> : null}
              {progress?.tools.map((tool) => <button type="button" key={tool.id} onClick={() => tool.logId && void showToolLog(tool.logId)} disabled={!tool.logId}>{tool.name} · {tool.status}</button>)}
              {progress?.todoItems.map((item, index) => <div key={index}>Todo · {activitySummary(item as Record<string, unknown>)}</div>)}
              {progress?.contextUsage ? <div>Context · {activitySummary(progress.contextUsage)}</div> : null}
              {progress?.interruptStatus ? <div>Interrupt · {progress.interruptStatus}</div> : null}
            </details>
            <details><summary>Workers ({teamMembers.length + (progress?.subagents.length ?? 0)})</summary>{teamMembers.map((member) => <button type="button" key={member.name} onClick={() => void showTeamLog(member.name)}>{member.name} · {member.status ?? member.activity ?? "active"}</button>)}{progress?.subagents.map((worker, index) => <div key={index}>{activitySummary(worker)}</div>)}</details>
            <details><summary>Tasks ({tasks.length})</summary>{tasks.map((task) => <div key={task.id}>#{task.id} {task.subject ?? task.description} · {task.status}</div>)}</details>
            <details><summary>Tool logs ({toolLogs.length})</summary>{toolLogs.map((log) => <button type="button" key={log.id} onClick={() => void showToolLog(log.id)}>{log.tool_name} · {log.actor}</button>)}</details>
            {diagnosticDetail ? <ContextPanel className="remote-diagnostic-panel" ariaLabel="Diagnostic detail"><details open><summary>Diagnostic detail</summary><pre>{diagnosticDetail}</pre></details></ContextPanel> : null}
          </ProgressPanel> : null}
          <ConversationComposer value={draft} placeholder="Ask Somnia" disabled={!session || access.busy}>
            <div className="remote-composer-context" aria-label="Message target">
              Sending to <strong>{selectedDevice?.name ?? "No computer"}</strong> / <strong>{projects.find((project) => project.project_id === projectId)?.name ?? projectId}</strong>
              {connectionState !== "connected" ? <span>Draft is kept locally until the computer is connected.</span> : null}
            </div>
            <div className="remote-composer-input">
              {pendingImages.length ? <div className="remote-image-previews" aria-label="Pending images">{pendingImages.map((image) => <span key={image.id}><img src={image.dataUrl} alt={image.name} /><button type="button" onClick={() => setPendingImages((current) => current.filter((item) => item.id !== image.id))} aria-label={`Remove ${image.name}`}>×</button></span>)}</div> : null}
              {commandPickerOpen ? <div className="remote-suggestion-picker" role="listbox" aria-label="Slash commands">{REMOTE_COMMANDS.filter((command) => command.startsWith(draft.trimStart())).map((command, index) => <button type="button" key={command} className={index === selectedSuggestionIndex ? "selected" : ""} onMouseDown={(event) => event.preventDefault()} onClick={() => chooseCommand(command)}>{command}</button>)}</div> : null}
              {pathPickerOpen ? <div className="remote-suggestion-picker" role="listbox" aria-label="Project paths">{pathSuggestions.map((item, index) => <button type="button" key={item.path} className={index === selectedSuggestionIndex ? "selected" : ""} onMouseDown={(event) => event.preventDefault()} onClick={() => choosePath(item.path)}><strong>{item.path}</strong><small>{item.kind}</small></button>)}</div> : null}
              <textarea value={draft} onChange={(event) => { setDraft(event.target.value); setComposerCursor(event.target.selectionStart); }} onSelect={(event) => setComposerCursor(event.currentTarget.selectionStart)} onPaste={(event) => void handleComposerPaste(event)} placeholder="Ask Somnia" disabled={!session || access.busy} onKeyDown={handleComposerKeyDown} />
            </div>
            <input ref={fileInputRef} type="file" accept="image/*" multiple hidden onChange={(event) => void handleImageFiles(event.target.files)} />
            <button type="button" onClick={() => fileInputRef.current?.click()} disabled={!session || !connected || access.busy} aria-label="Attach image">＋</button>
            <button type="button" onClick={() => void sendPrompt()} disabled={!session || !connected || (!draft.trim() && pendingImages.length === 0) || access.busy}>{progress?.activeTurnId ? "Queue" : "Send"}</button>
            {progress?.activeTurnId ? <button type="button" className="remote-interrupt-button" onClick={() => void interruptActiveTurn()}>Interrupt</button> : null}
          </ConversationComposer>
        </ConversationPanel>
      </ConversationWorkspace>
    </main>
  );
}

function RemoteHeader({ state, deviceStatus, deviceId, projectId }: { state: Parameters<typeof remoteConnectionCopy>[0]; deviceStatus: string; deviceId: string; projectId: string }) {
  const copy = remoteConnectionCopy(state);
  return <header className="remote-header"><div><strong>Somnia Remote</strong><span className={`remote-status remote-status-${state}`} title={copy.action}>{copy.label}</span></div><span className="remote-project-label">{deviceId ? `${deviceId} / ${projectId} (${deviceStatus})` : projectId}</span></header>;
}

function readConnectionDefaults() {
  const params = new URLSearchParams(window.location.search);
  const sameOrigin = window.location.protocol === "http:" || window.location.protocol === "https:";
  return { relayUrl: params.get("relay") ?? (sameOrigin ? window.location.origin : "http://127.0.0.1:8787"), projectId: params.get("project") ?? "default-project" };
}

function currentRemotePathMention(value: string, cursor: number): { query: string; queryStart: number; end: number } | null {
  const before = value.slice(0, Math.max(0, Math.min(cursor, value.length)));
  const match = /(^|\s)@([^\s]*)$/.exec(before);
  if (!match) return null;
  const query = match[2] ?? "";
  return { query, queryStart: before.length - query.length, end: before.length };
}

async function buildRemotePromptPayload(connection: RemoteSomniaConnection, prompt: string, images: RemotePendingImage[]): Promise<string | Record<string, unknown>> {
  if (images.length === 0) return prompt;
  const staged = await Promise.all(images.map((image) => connection.stageInlineImage({ name: image.name, mediaType: image.mediaType, dataUrl: image.dataUrl })));
  return {
    role: "user",
    content: [
      ...(prompt.trim() ? [{ type: "text", text: prompt.trim() }] : []),
      ...staged.map((image) => ({ type: "input_image", path: image.path, absolute_path: image.absolute_path, media_type: image.media_type })),
    ],
  };
}

function readRemoteImage(file: File): Promise<RemotePendingImage> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ id: createRequestId("image"), name: file.name || "pasted-image", mediaType: file.type, dataUrl: String(reader.result ?? "") });
    reader.onerror = () => reject(reader.error ?? new Error("Unable to read image."));
    reader.readAsDataURL(file);
  });
}

function remoteHistoryKey(deviceId: string, projectId: string): string {
  return `somnia.remote.prompt-history:${deviceId}:${projectId}`;
}

function remoteDraftKey(deviceId: string, projectId: string, sessionId: string): string {
  return `somnia.remote.draft:${deviceId}:${projectId}:${sessionId}`;
}

function isSameOriginRelay(value: string): boolean {
  try { return new URL(value).origin === window.location.origin; }
  catch { return false; }
}

function remoteLastSessionKey(deviceId: string, projectId: string): string {
  return `somnia.remote.last-session:${deviceId}:${projectId}`;
}

function readRemoteLastSession(deviceId: string, projectId: string): string {
  if (!deviceId || !projectId) return "";
  return localStorage.getItem(remoteLastSessionKey(deviceId, projectId)) ?? "";
}

function rememberRemoteLastSession(deviceId: string, projectId: string, sessionId: string): void {
  if (!deviceId || !projectId || !sessionId) return;
  localStorage.setItem(remoteLastSessionKey(deviceId, projectId), sessionId);
}

function readRemotePromptHistory(deviceId: string, projectId: string): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(remoteHistoryKey(deviceId, projectId)) ?? "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(-100) : [];
  } catch { return []; }
}

function rememberRemotePrompt(deviceId: string, projectId: string, prompt: string, setHistory: (value: string[]) => void): void {
  const normalized = prompt.trim();
  if (!normalized) return;
  const next = [...readRemotePromptHistory(deviceId, projectId).filter((item) => item !== normalized), normalized].slice(-100);
  localStorage.setItem(remoteHistoryKey(deviceId, projectId), JSON.stringify(next));
  setHistory(next);
}

function readRemoteDraft(deviceId: string, projectId: string, sessionId: string): string {
  if (!sessionId) return "";
  return localStorage.getItem(remoteDraftKey(deviceId, projectId, sessionId)) ?? "";
}

function writeRemoteDraft(deviceId: string, projectId: string, sessionId: string, draft: string): void {
  if (!sessionId) return;
  const key = remoteDraftKey(deviceId, projectId, sessionId);
  if (draft) localStorage.setItem(key, draft);
  else localStorage.removeItem(key);
}

function connectionStateMessage(state: string): string {
  if (state === "connected") return "Computer connection is ready.";
  if (state === "connecting") return "Connecting to computer...";
  if (state === "error") return "Computer connection failed. Your draft is kept locally.";
  return "Computer disconnected. Your draft is kept locally.";
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function archiveStorageKey(deviceId: string, projectId: string): string {
  return `somnia.remote.archived-sessions:${deviceId}:${projectId}`;
}

function activitySummary(payload: Record<string, unknown>): string {
  return String(payload.content ?? payload.subject ?? payload.tool_name ?? payload.name ?? payload.message ?? payload.status ?? payload.used_tokens ?? "updated");
}

function createRequestId(prefix: string): string {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`}`;
}
