import { describe, expect, it } from "vitest";

import type { AgentSession, SidecarEvent } from "../types";
import { describeSomniaConnectionContract } from "./somnia-connection.contract";
import { RemoteSomniaConnection } from "./remote-somnia-connection";
import type { SomniaConnectionNotification } from "./somnia-connection";

const loadedSession: AgentSession = {
  id: "session-1",
  messages: [{ role: "user", content: "Question" }],
  token_usage: {},
  todo_items: [],
  rounds_without_todo: 0,
};

class FakeRelaySocket {
  onopen: ((event: Event) => unknown) | null = null;
  onclose: ((event: CloseEvent) => unknown) | null = null;
  onerror: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent) => unknown) | null = null;

  open() {
    this.onopen?.(new Event("open"));
  }

  send(rawMessage: string) {
    const request = JSON.parse(rawMessage) as { request_id: string; method: string };
    const result =
      request.method === "turn.start"
        ? { turn_id: "turn-1", session_id: loadedSession.id }
        : loadedSession;
    this.emit({ kind: "response", request_id: request.request_id, ok: true, result });
  }

  emit(message: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(message) }));
  }

  close() {
    this.onclose?.(new Event("close") as CloseEvent);
  }
}

function createHarness() {
  const socket = new FakeRelaySocket();
  return {
    socket,
    connection: new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      socketFactory: () => socket,
    }),
    openStream: () => socket.open(),
    emitEvent: (event: SidecarEvent) => socket.emit({ kind: "event", project_id: "project-1", event }),
    closeStream: () => socket.close(),
  };
}

describeSomniaConnectionContract("Remote", createHarness, loadedSession);

describe("Remote Somnia Connection", () => {
  it("reloads the authoritative Session before publishing Turn completion", async () => {
    const { connection, socket, openStream } = createHarness();
    const notifications: SomniaConnectionNotification[] = [];
    connection.subscribe((notification) => notifications.push(notification));
    openStream();

    socket.emit({
      kind: "event",
      project_id: "project-1",
      event: {
        type: "turn_result",
        session_id: loadedSession.id,
        turn_id: "turn-1",
        payload: { session: { ...loadedSession, messages: [] } },
      },
    });
    await Promise.resolve();

    const completion = notifications.find(
      (notification) => notification.kind === "event" && notification.event.type === "turn_result",
    );
    expect(completion).toEqual({
      kind: "event",
      event: {
        type: "turn_result",
        session_id: loadedSession.id,
        turn_id: "turn-1",
        payload: { session: loadedSession },
      },
    });
  });
});
