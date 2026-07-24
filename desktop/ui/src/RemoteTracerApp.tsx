import { useEffect, useRef, useState } from "react";

import type { AgentSession } from "./types";
import { createConversationState, transitionConversationEvent, type ConversationState } from "./lib/conversation-state";
import { extractTextContent } from "./lib/messages";
import { RemoteSomniaConnection } from "./lib/remote-somnia-connection";
import type { SomniaConnectionNotification } from "./lib/somnia-connection";
import { useRemoteAccess } from "./lib/use-remote-access";

const defaults = readConnectionDefaults();

export default function RemoteTracerApp() {
  const access = useRemoteAccess(defaults.relayUrl);
  const [projectId, setProjectId] = useState(defaults.projectId);
  const [connectionState, setConnectionState] = useState("disconnected");
  const [session, setSession] = useState<AgentSession | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingPrompt, setPendingPrompt] = useState("");
  const [streamingText, setStreamingText] = useState("");
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
      setConnectionState("disconnected");
    }
  }

  async function signOut() {
    await access.signOut();
    connectionRef.current?.close();
    connectionRef.current = null;
    conversationRef.current = null;
    setSession(null);
    setConnectionState("disconnected");
  }

  async function connect() {
    if (!access.deviceId) return;
    setConversationBusy(true);
    try {
      if (!await access.verifyAccess()) return;
      connectionRef.current?.close();
      setSession(null);
      setPendingPrompt("");
      setStreamingText("");
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
            conversationRef.current = createConversationState(recovered);
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
    if (transition.effect.type === "turn_started") {
      setStreamingText("");
    } else if (transition.effect.type === "assistant_delta") {
      setStreamingText(transition.state.assistantText);
    } else if (transition.effect.type === "turn_completed") {
      if (transition.state.session) setSession(transition.state.session);
      setPendingPrompt("");
      setStreamingText("");
      setConversationBusy(false);
    }
  }

  async function createSession() {
    const connection = connectionRef.current;
    if (!connection) return;
    setConversationBusy(true);
    try {
      const created = await connection.execute({ type: "session.create" });
      conversationRef.current = createConversationState(created);
      setSession(created);
      access.setNotice(`Session ${created.id} is ready.`);
    } catch (error) {
      access.setNotice(formatError(error));
    } finally {
      setConversationBusy(false);
    }
  }

  async function sendPrompt() {
    const connection = connectionRef.current;
    const prompt = draft.trim();
    if (!connection || !session || !prompt || busy) return;
    setConversationBusy(true);
    setDraft("");
    setPendingPrompt(prompt);
    setStreamingText("");
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

  function switchTarget(deviceId: string, nextProjectId: string) {
    connectionRef.current?.close();
    connectionRef.current = null;
    conversationRef.current = null;
    setSession(null);
    setPendingPrompt("");
    setStreamingText("");
    setConnectionState("disconnected");
    access.setDeviceId(deviceId);
    setProjectId(nextProjectId);
  }
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
          {session ? <button type="button" className="remote-session-row remote-session-row-selected"><strong>{session.id}</strong><span>{session.messages.length} messages</span></button> : <p className="remote-empty">No active Session</p>}
        </aside>
        <section className="remote-conversation" aria-label="Conversation">
          <div className="remote-messages">
            {session?.messages.map((message, index) => <article className={`remote-message remote-message-${message.role}`} key={`${message.role}-${index}`}><span>{message.role}</span><p>{extractTextContent(message.content)}</p></article>)}
            {pendingPrompt ? <article className="remote-message remote-message-user remote-message-pending"><span>user</span><p>{pendingPrompt}</p></article> : null}
            {streamingText ? <article className="remote-message remote-message-assistant remote-message-streaming"><span>assistant</span><p>{streamingText}</p></article> : null}
            {!session ? <p className="remote-conversation-empty">Create a Session to begin.</p> : null}
          </div>
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
