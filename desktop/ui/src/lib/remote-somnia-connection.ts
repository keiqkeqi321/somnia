import type { AgentSession, SidecarEvent, TurnStartResponse } from "../types";
import type {
  SessionCreateCommand,
  SessionLoadQuery,
  SomniaConnection,
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
}

export interface RemoteSomniaConnectionOptions {
  relayUrl: string;
  deviceId: string;
  projectId: string;
  socketFactory?: (url: string) => RemoteSocket;
}

export class RemoteSomniaConnection implements SomniaConnection {
  private readonly relayUrl: string;
  private readonly deviceId: string;
  private readonly projectId: string;
  private readonly socketFactory: (url: string) => RemoteSocket;
  private readonly listeners = new Set<SomniaConnectionListener>();
  private readonly pending = new Map<string, PendingRequest>();
  private socket: RemoteSocket | null = null;
  private state: SomniaConnectionState = "disconnected";
  private requestSequence = 0;

  constructor(options: RemoteSomniaConnectionOptions) {
    this.relayUrl = normalizeRelayUrl(options.relayUrl);
    this.deviceId = required(options.deviceId, "deviceId");
    this.projectId = required(options.projectId, "projectId");
    this.socketFactory = options.socketFactory ?? ((url) => new WebSocket(url));
  }

  query(query: SessionLoadQuery): Promise<AgentSession> {
    return this.sendRequest("session.load", { session_id: query.sessionId });
  }

  execute(command: SessionCreateCommand): Promise<AgentSession>;
  execute(command: TurnStartCommand): Promise<TurnStartResponse>;
  execute(command: SessionCreateCommand | TurnStartCommand): Promise<AgentSession | TurnStartResponse> {
    if (command.type === "session.create") {
      return this.sendRequest("session.create", {});
    }
    return this.sendRequest("turn.start", { session_id: command.sessionId, user_input: command.userInput });
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
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.setState("disconnected");
    this.rejectPending("Remote connection closed.");
    this.listeners.clear();
  }

  private open(): void {
    this.setState("connecting");
    const url = `${this.relayUrl}/ws/client/${encodeURIComponent(this.deviceId)}`;
    const socket = this.socketFactory(url);
    this.socket = socket;
    socket.onopen = () => this.setState("connected");
    socket.onerror = () => this.setState("error", "Relay connection failed.");
    socket.onclose = () => {
      if (this.socket === socket) {
        this.socket = null;
      }
      this.setState("disconnected");
      this.rejectPending("Relay connection closed.");
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
    if (message.kind !== "event" || message.project_id !== this.projectId || !isSidecarEvent(message.event)) {
      return;
    }
    const event = message.event;
    if (event.type === "turn_result" && event.session_id) {
      void this.publishAuthoritativeCompletion(event);
      return;
    }
    this.notify({ kind: "event", event });
  }

  private async publishAuthoritativeCompletion(event: SidecarEvent): Promise<void> {
    try {
      const session = await this.query({ type: "session.load", sessionId: String(event.session_id) });
      this.notify({ kind: "event", event: { ...event, payload: { ...event.payload, session } } });
    } catch (error) {
      this.notify({ kind: "protocol_error", error: `Unable to reload completed Session: ${formatError(error)}` });
    }
  }

  private sendRequest<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const socket = this.socket;
    if (!socket || this.state !== "connected") {
      return Promise.reject(new Error("Remote Somnia connection is not connected."));
    }
    const requestId = `web-${Date.now().toString(36)}-${++this.requestSequence}`;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(requestId, {
        resolve: (value) => resolve(value as T),
        reject,
      });
      socket.send(
        JSON.stringify({
          kind: "request",
          request_id: requestId,
          project_id: this.projectId,
          method,
          params,
        }),
      );
    });
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
