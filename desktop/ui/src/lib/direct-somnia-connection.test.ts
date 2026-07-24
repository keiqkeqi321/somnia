import { describeSomniaConnectionContract } from "./somnia-connection.contract";
import { DirectSomniaConnection } from "./somnia-connection";
import type { AgentSession, SidecarEvent } from "../types";

const loadedSession: AgentSession = {
  id: "session-1",
  messages: [{ role: "user", content: "Initial question" }],
  token_usage: {},
  todo_items: [],
  rounds_without_todo: 0,
};

class FakeEventSocket {
  onopen: ((event: Event) => unknown) | null = null;
  onclose: ((event: CloseEvent) => unknown) | null = null;
  onerror: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent) => unknown) | null = null;

  open() {
    this.onopen?.(new Event("open"));
  }

  emit(event: SidecarEvent) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
  }

  close() {
    this.onclose?.(new Event("close") as CloseEvent);
  }
}

describeSomniaConnectionContract(
  "Direct",
  () => {
    const socket = new FakeEventSocket();
    const client = {
    createSession: async () => loadedSession,
    listSessions: async () => [loadedSession],
    loadSession: async () => loadedSession,
    deleteSession: async (sessionId: string) => ({ session_id: sessionId, deleted: true }),
      startTurn: async () => ({ turn_id: "turn-1", session_id: loadedSession.id }),
      createEventSocket: () => socket,
    };
    return {
      connection: new DirectSomniaConnection(client, "ws://sidecar.test/ws"),
      openStream: () => socket.open(),
      emitEvent: (event) => socket.emit(event),
      closeStream: () => socket.close(),
    };
  },
  loadedSession,
);
