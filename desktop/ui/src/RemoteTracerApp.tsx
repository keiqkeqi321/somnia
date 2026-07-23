import { useRef, useState } from "react";

import type { AgentSession } from "./types";
import { createConversationState, transitionConversationEvent, type ConversationState } from "./lib/conversation-state";
import { extractTextContent } from "./lib/messages";
import { RemoteSomniaConnection } from "./lib/remote-somnia-connection";

const defaults = readConnectionDefaults();

export default function RemoteTracerApp() {
  const [relayUrl, setRelayUrl] = useState(defaults.relayUrl);
  const [deviceId, setDeviceId] = useState(defaults.deviceId);
  const [projectId, setProjectId] = useState(defaults.projectId);
  const [connectionState, setConnectionState] = useState("disconnected");
  const [connectionMessage, setConnectionMessage] = useState("Enter the Relay and local Connector identity.");
  const [session, setSession] = useState<AgentSession | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingPrompt, setPendingPrompt] = useState("");
  const [streamingText, setStreamingText] = useState("");
  const [busy, setBusy] = useState(false);
  const connectionRef = useRef<RemoteSomniaConnection | null>(null);
  const conversationRef = useRef<ConversationState | null>(null);

  function connect() {
    connectionRef.current?.close();
    setSession(null);
    setPendingPrompt("");
    setStreamingText("");
    const connection = new RemoteSomniaConnection({ relayUrl, deviceId, projectId });
    connectionRef.current = connection;
    connection.subscribe((notification) => {
      if (notification.kind === "state") {
        setConnectionState(notification.state);
        setConnectionMessage(notification.error ?? connectionStateMessage(notification.state));
        return;
      }
      if (notification.kind === "protocol_error") {
        setConnectionMessage(notification.error);
        return;
      }
      const event = notification.event;
      if (!event.session_id) {
        return;
      }
      const previous = conversationRef.current ?? createConversationState(event.session_id);
      const transition = transitionConversationEvent(previous, event);
      conversationRef.current = transition.state;
      if (transition.effect.type === "turn_started") {
        setStreamingText("");
      } else if (transition.effect.type === "assistant_delta") {
        setStreamingText(transition.state.assistantText);
      } else if (transition.effect.type === "turn_completed") {
        if (transition.state.session) {
          setSession(transition.state.session);
        }
        setPendingPrompt("");
        setStreamingText("");
        setBusy(false);
      }
    });
  }

  async function createSession() {
    const connection = connectionRef.current;
    if (!connection) {
      return;
    }
    setBusy(true);
    try {
      const created = await connection.execute({ type: "session.create" });
      conversationRef.current = createConversationState(created);
      setSession(created);
      setConnectionMessage(`Session ${created.id} is ready.`);
    } catch (error) {
      setConnectionMessage(formatError(error));
    } finally {
      setBusy(false);
    }
  }

  async function sendPrompt() {
    const connection = connectionRef.current;
    const prompt = draft.trim();
    if (!connection || !session || !prompt || busy) {
      return;
    }
    setBusy(true);
    setDraft("");
    setPendingPrompt(prompt);
    setStreamingText("");
    try {
      await connection.execute({ type: "turn.start", sessionId: session.id, userInput: prompt });
    } catch (error) {
      setBusy(false);
      setPendingPrompt("");
      setConnectionMessage(formatError(error));
    }
  }

  const connected = connectionState === "connected";
  return (
    <main className="remote-shell">
      <header className="remote-header">
        <div>
          <strong>Somnia Remote</strong>
          <span className={`remote-status remote-status-${connectionState}`}>{connectionState}</span>
        </div>
        <span className="remote-project-label">{deviceId} / {projectId}</span>
      </header>

      <section className="remote-connection" aria-label="Remote connection">
        <label>
          Relay
          <input value={relayUrl} onChange={(event) => setRelayUrl(event.target.value)} disabled={connected} />
        </label>
        <label>
          Device
          <input value={deviceId} onChange={(event) => setDeviceId(event.target.value)} disabled={connected} />
        </label>
        <label>
          Project
          <input value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={connected} />
        </label>
        <button type="button" onClick={connect}>{connected ? "Reconnect" : "Connect"}</button>
      </section>

      <div className="remote-notice" role="status">{connectionMessage}</div>

      <section className="remote-workspace">
        <aside className="remote-session-pane">
          <div className="remote-pane-heading">
            <span>Session</span>
            <button type="button" onClick={() => void createSession()} disabled={!connected || busy}>New</button>
          </div>
          {session ? (
            <button type="button" className="remote-session-row remote-session-row-selected">
              <strong>{session.id}</strong>
              <span>{session.messages.length} messages</span>
            </button>
          ) : (
            <p className="remote-empty">No active Session</p>
          )}
        </aside>

        <section className="remote-conversation" aria-label="Conversation">
          <div className="remote-messages">
            {session?.messages.map((message, index) => (
              <article className={`remote-message remote-message-${message.role}`} key={`${message.role}-${index}`}>
                <span>{message.role}</span>
                <p>{extractTextContent(message.content)}</p>
              </article>
            ))}
            {pendingPrompt ? (
              <article className="remote-message remote-message-user remote-message-pending">
                <span>user</span>
                <p>{pendingPrompt}</p>
              </article>
            ) : null}
            {streamingText ? (
              <article className="remote-message remote-message-assistant remote-message-streaming">
                <span>assistant</span>
                <p>{streamingText}</p>
              </article>
            ) : null}
            {!session ? <p className="remote-conversation-empty">Connect and create a Session to begin.</p> : null}
          </div>
          <div className="remote-composer">
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Ask Somnia"
              disabled={!session || !connected || busy}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void sendPrompt();
                }
              }}
            />
            <button type="button" onClick={() => void sendPrompt()} disabled={!session || !draft.trim() || busy}>Send</button>
          </div>
        </section>
      </section>
    </main>
  );
}

function readConnectionDefaults() {
  const params = new URLSearchParams(window.location.search);
  return {
    relayUrl: params.get("relay") ?? "ws://127.0.0.1:8787",
    deviceId: params.get("device") ?? "local-device",
    projectId: params.get("project") ?? "default-project",
  };
}

function connectionStateMessage(state: string): string {
  if (state === "connected") return "Relay connected. The selected Project is available.";
  if (state === "connecting") return "Connecting to Relay...";
  if (state === "error") return "Relay connection failed.";
  return "Relay disconnected.";
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
