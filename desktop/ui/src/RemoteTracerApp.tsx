import { useEffect, useRef, useState } from "react";

import type { AgentSession, ConversationRuntimeItem, TaskGraphItem, TeamMemberActivity, ToolLogIndexEntry } from "./types";
import { createConversationState, transitionConversationEvent, type ConversationState } from "./lib/conversation-state";
import { buildConversationRows, stringifyToolValue } from "./lib/messages";
import { RemoteSomniaConnection } from "./lib/remote-somnia-connection";
import type { SomniaConnectionNotification } from "./lib/somnia-connection";
import { useRemoteAccess } from "./lib/use-remote-access";
import { RemoteConversationRow } from "./RemoteRichContent";

const defaults = readConnectionDefaults();

export default function RemoteTracerApp() {
  const access = useRemoteAccess(defaults.relayUrl);
  const [projectId, setProjectId] = useState(defaults.projectId);
  const [connectionState, setConnectionState] = useState("disconnected");
  const [session, setSession] = useState<AgentSession | null>(null);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [archivedSessionIds, setArchivedSessionIds] = useState<Set<string>>(() => new Set());
  const [draft, setDraft] = useState("");
  const [pendingPrompt, setPendingPrompt] = useState("");
  const [progress, setProgress] = useState<ConversationState | null>(null);
  const [teamMembers, setTeamMembers] = useState<TeamMemberActivity[]>([]);
  const [tasks, setTasks] = useState<TaskGraphItem[]>([]);
  const [toolLogs, setToolLogs] = useState<ToolLogIndexEntry[]>([]);
  const [diagnosticDetail, setDiagnosticDetail] = useState("");
  const [conversationBusy, setConversationBusy] = useState(false);
  const connectionRef = useRef<RemoteSomniaConnection | null>(null);
  const conversationRef = useRef<ConversationState | null>(null);

  const busy = access.busy || conversationBusy;

  async function revokeSelectedDevice() {
    if (await access.revokeSelectedDevice()) {
      connectionRef.current?.close();
      connectionRef.current = null;
      conversationRef.current = null;
      setSession(null);
      setSessions([]);
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
      access.setNotice(notification.error ?? connectionStateMessage(notification.state));
      if (notification.state === "connected") {
        void connectionRef.current?.query({ type: "session.list" }).then((loaded) => setSessions(loaded)).catch((error) => access.setNotice(formatError(error)));
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
      const currentSessionId = conversationRef.current?.session?.id ?? session?.id;
      if (Array.isArray(sessions) && currentSessionId) {
        const knownSession = sessions.find((candidate) => (
          candidate && typeof candidate === "object" && (candidate as { id?: unknown }).id === currentSessionId
        ));
        if (knownSession && connectionRef.current) {
          void connectionRef.current.query({ type: "session.load", sessionId: currentSessionId }).then((recovered) => {
            setSession(recovered);
            const recoveredState = createConversationState(recovered);
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
    if (transition.effect.type === "turn_started") {
    } else if (transition.effect.type === "turn_completed") {
      if (transition.state.session) setSession(transition.state.session);
      setPendingPrompt("");
      setConversationBusy(false);
      void refreshExecutionDetails(event.session_id);
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
      conversationRef.current = createConversationState(loaded);
      setProgress(conversationRef.current);
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
        clearExecutionDetails();
        conversationRef.current = null;
      }
    } catch (error) { access.setNotice(formatError(error)); }
  }

  async function sendPrompt() {
    const connection = connectionRef.current;
    const prompt = draft.trim();
    if (!connection || !session || !prompt || busy) return;
    setConversationBusy(true);
    setDraft("");
    setPendingPrompt(prompt);
    setProgress(conversationRef.current);
    try {
      await connection.execute({ type: "turn.start", sessionId: session.id, userInput: prompt });
    } catch (error) {
      setConversationBusy(false);
      setPendingPrompt("");
      access.setNotice(formatError(error));
    }
  }

  const connected = connectionState === "connected";
  const selectedDevice = access.devices.find((device) => device.device_id === access.deviceId);
  const projects = selectedDevice?.projects ?? [];

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
    setProgress(null);
    clearExecutionDetails();
    setConnectionState("disconnected");
    access.setDeviceId(deviceId);
    setProjectId(nextProjectId);
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
  ] : [];
  const conversationRows = buildConversationRows(session, runtimeItems, pendingPrompt ? { id: "remote-pending", sessionId: session?.id ?? null, userText: pendingPrompt, placeholderText: "Working…" } : null, progress?.activeTurnId ? session?.messages.length ?? 0 : null);
  if (!access.authenticated) {
    return (
      <main className="remote-shell remote-shell-login">
        <RemoteHeader state={connectionState} deviceId="" projectId={projectId} />
        <form className="remote-login" onSubmit={(event) => { event.preventDefault(); void access.signIn(); }}>
          <h1>Somnia Remote</h1>
          <label>Relay<input value={access.relayUrl} onChange={(event) => access.setRelayUrl(event.target.value)} /></label>
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
      <RemoteHeader state={connectionState} deviceId={access.deviceId} projectId={projectId} />
      <section className="remote-connection" aria-label="Remote connection">
        <label>Device
          <select aria-label="Device" value={access.deviceId} onChange={(event) => {
            const device = access.devices.find((candidate) => candidate.device_id === event.target.value);
            switchTarget(event.target.value, device?.projects[0]?.project_id ?? "");
          }} disabled={connected}>
            <option value="">Select Device</option>
            {access.devices.filter((device) => !device.revoked_at).map((device) => <option key={device.device_id} value={device.device_id}>{device.name} ({device.status})</option>)}
          </select>
        </label>
        <label>Project
          <select aria-label="Project" value={projectId} onChange={(event) => switchTarget(access.deviceId, event.target.value)} disabled={connected || projects.length === 0}>
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
        {access.pairingCode ? <output className="remote-pairing-code">{access.pairingCode}</output> : null}
      </section>
      <div className="remote-notice" role="status">{access.notice}</div>
      <section className="remote-workspace">
        <aside className="remote-session-pane">
          <div className="remote-pane-heading"><span>Session</span><button type="button" onClick={() => void createSession()} disabled={!connected || busy}>New</button></div>
          {sessions.filter((candidate) => !archivedSessionIds.has(candidate.id)).map((candidate) => <div className={`remote-session-row ${session?.id === candidate.id ? "remote-session-row-selected" : ""}`} key={candidate.id}><button type="button" onClick={() => void selectSession(candidate.id)}><strong>{candidate.preview ?? candidate.id}</strong><span>{candidate.messages.length} messages</span></button><button type="button" onClick={() => setArchived(candidate.id)}>Archive</button><button type="button" onClick={() => void deleteSession(candidate.id)}>Delete</button></div>)}
          {Array.from(archivedSessionIds).length ? <button type="button" onClick={restoreArchived}>Restore archived</button> : null}
          {!sessions.length ? <p className="remote-empty">No active Session</p> : null}
        </aside>
        <section className="remote-conversation" aria-label="Conversation">
          <div className="remote-messages">
            {conversationRows.map((row) => <RemoteConversationRow key={row.id} row={row} resolveImage={(path) => {
              const connection = connectionRef.current;
              return connection ? connection.getWorkspaceImage(path) : Promise.reject(new Error("Remote Device is offline."));
            }} />)}
            {!session ? <p className="remote-conversation-empty">Create a Session to begin.</p> : null}
          </div>
          {session ? <aside className="remote-progress" aria-label="Execution progress">
            <details open><summary>Progress</summary>
              {progress?.thinking ? <button type="button" onClick={() => progress.thinking?.path && void showThinkingLog(progress.thinking.path)} disabled={!progress.thinking.path}>Thinking · {progress.thinking.status}</button> : null}
              {progress?.tools.map((tool) => <button type="button" key={tool.id} onClick={() => tool.logId && void showToolLog(tool.logId)} disabled={!tool.logId}>{tool.name} · {tool.status}</button>)}
              {progress?.todoItems.map((item, index) => <div key={index}>Todo · {activitySummary(item as Record<string, unknown>)}</div>)}
              {progress?.contextUsage ? <div>Context · {activitySummary(progress.contextUsage)}</div> : null}
            </details>
            <details><summary>Workers ({teamMembers.length + (progress?.subagents.length ?? 0)})</summary>{teamMembers.map((member) => <button type="button" key={member.name} onClick={() => void showTeamLog(member.name)}>{member.name} · {member.status ?? member.activity ?? "active"}</button>)}{progress?.subagents.map((worker, index) => <div key={index}>{activitySummary(worker)}</div>)}</details>
            <details><summary>Tasks ({tasks.length})</summary>{tasks.map((task) => <div key={task.id}>#{task.id} {task.subject ?? task.description} · {task.status}</div>)}</details>
            <details><summary>Tool logs ({toolLogs.length})</summary>{toolLogs.map((log) => <button type="button" key={log.id} onClick={() => void showToolLog(log.id)}>{log.tool_name} · {log.actor}</button>)}</details>
            {diagnosticDetail ? <details open><summary>Diagnostic detail</summary><pre>{diagnosticDetail}</pre></details> : null}
          </aside> : null}
          <div className="remote-composer">
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Ask Somnia" disabled={!session || !connected || busy} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendPrompt(); } }} />
            <button type="button" onClick={() => void sendPrompt()} disabled={!session || !draft.trim() || busy}>Send</button>
          </div>
        </section>
      </section>
    </main>
  );
}

function RemoteHeader({ state, deviceId, projectId }: { state: string; deviceId: string; projectId: string }) {
  return <header className="remote-header"><div><strong>Somnia Remote</strong><span className={`remote-status remote-status-${state}`}>{state}</span></div><span className="remote-project-label">{deviceId ? `${deviceId} / ${projectId}` : projectId}</span></header>;
}

function readConnectionDefaults() {
  const params = new URLSearchParams(window.location.search);
  return { relayUrl: params.get("relay") ?? "ws://127.0.0.1:8787", projectId: params.get("project") ?? "default-project" };
}

function connectionStateMessage(state: string): string {
  if (state === "connected") return "Relay connected.";
  if (state === "connecting") return "Connecting to Relay...";
  if (state === "error") return "Relay connection failed.";
  return "Relay disconnected.";
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
