import type { AgentSession, SidecarEvent, TurnStartResponse } from "../types";

export type SomniaConnectionState = "connecting" | "connected" | "disconnected" | "error";

export type SomniaConnectionNotification =
  | { kind: "state"; state: SomniaConnectionState; error?: string }
  | { kind: "event"; event: SidecarEvent }
  | { kind: "protocol_error"; error: string };

export type SomniaConnectionListener = (notification: SomniaConnectionNotification) => void;

export interface SessionLoadQuery {
  type: "session.load";
  sessionId: string;
}

export interface TurnStartCommand {
  type: "turn.start";
  sessionId: string;
  userInput: string | Record<string, unknown>;
}

export interface SomniaConnection {
  query(query: SessionLoadQuery): Promise<AgentSession>;
  execute(command: TurnStartCommand): Promise<TurnStartResponse>;
  subscribe(listener: SomniaConnectionListener): () => void;
  connectionState(): SomniaConnectionState;
  close(): void;
}

interface SomniaEventSocket {
  onopen: ((event: Event) => unknown) | null;
  onclose: ((event: CloseEvent) => unknown) | null;
  onerror: ((event: Event) => unknown) | null;
  onmessage: ((event: MessageEvent) => unknown) | null;
  close(): void;
}

export interface DirectSomniaClient {
  loadSession(sessionId: string): Promise<AgentSession>;
  startTurn(sessionId: string, userInput: string | Record<string, unknown>): Promise<TurnStartResponse>;
  createEventSocket(wsUrl?: string): SomniaEventSocket;
}

export class DirectSomniaConnection implements SomniaConnection {
  private readonly listeners = new Set<SomniaConnectionListener>();
  private socket: SomniaEventSocket | null = null;
  private state: SomniaConnectionState = "disconnected";

  constructor(
    private readonly client: DirectSomniaClient,
    private readonly wsUrl?: string,
  ) {}

  query(query: SessionLoadQuery): Promise<AgentSession> {
    return this.client.loadSession(query.sessionId);
  }

  execute(command: TurnStartCommand): Promise<TurnStartResponse> {
    return this.client.startTurn(command.sessionId, command.userInput);
  }

  subscribe(listener: SomniaConnectionListener): () => void {
    this.listeners.add(listener);
    if (!this.socket) {
      this.openEventStream();
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
    this.listeners.clear();
  }

  private openEventStream(): void {
    this.setState("connecting");
    const socket = this.client.createEventSocket(this.wsUrl);
    this.socket = socket;
    socket.onopen = () => this.setState("connected");
    socket.onclose = () => {
      if (this.socket === socket) {
        this.socket = null;
      }
      this.setState("disconnected");
    };
    socket.onerror = () => this.setState("error", "Sidecar event stream failed.");
    socket.onmessage = (messageEvent) => {
      try {
        const event = JSON.parse(String(messageEvent.data)) as SidecarEvent;
        this.notify({ kind: "event", event });
      } catch (error) {
        this.notify({ kind: "protocol_error", error: `Ignored malformed sidecar event: ${formatError(error)}` });
      }
    };
  }

  private setState(state: SomniaConnectionState, error?: string): void {
    if (this.state === state && !error) {
      return;
    }
    this.state = state;
    this.notify({ kind: "state", state, ...(error ? { error } : {}) });
  }

  private notify(notification: SomniaConnectionNotification): void {
    for (const listener of this.listeners) {
      listener(notification);
    }
  }
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
