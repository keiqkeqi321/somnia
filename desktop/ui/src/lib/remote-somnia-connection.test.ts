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
    const result = request.method === "turn.start"
      ? { turn_id: "turn-1", session_id: loadedSession.id }
      : request.method === "session.list"
        ? { sessions: [loadedSession] }
        : request.method === "session.delete"
          ? { session_id: loadedSession.id, deleted: true }
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
  it("buffers reordered events, ignores duplicates, and acknowledges the contiguous prefix", () => {
    const socket = new RecordingRelaySocket();
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      socketFactory: () => socket,
    });
    const notifications: SomniaConnectionNotification[] = [];
    connection.subscribe((notification) => notifications.push(notification));
    socket.open();

    socket.emit({
      kind: "stream_replay",
      protocol_version: 1,
      device_id: "device-1",
      project_id: "project-1",
      stream_epoch: "epoch-1",
      events: [],
    });
    socket.emit(sequencedEvent(2, "second"));
    socket.emit({
      kind: "stream_replay",
      protocol_version: 1,
      device_id: "device-1",
      project_id: "project-1",
      stream_epoch: "epoch-1",
      events: [sequencedEvent(1, "first")],
    });
    socket.emit(sequencedEvent(1, "first"));

    expect(notifications.filter((notification) => notification.kind === "event")).toEqual([
      { kind: "event", event: { type: "assistant_delta", payload: { delta: "first" } } },
      { kind: "event", event: { type: "assistant_delta", payload: { delta: "second" } } },
    ]);
    const acknowledgements = socket.sent.filter((message) => message.kind === "stream_ack");
    expect(acknowledgements[acknowledgements.length - 1]).toMatchObject({
      stream_epoch: "epoch-1",
      sequence: 2,
    });
    expect(socket.sent.some((message) => message.kind === "stream_resume" && message.after_sequence === 0)).toBe(true);
    connection.close();
  });

  it("reconnects and resends an unresolved command with the same request identity", async () => {
    const sockets: RecordingRelaySocket[] = [];
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      reconnectDelayMs: 0,
      socketFactory: () => {
        const socket = new RecordingRelaySocket();
        sockets.push(socket);
        return socket;
      },
    });
    connection.subscribe(() => undefined);
    sockets[0].open();
    const pending = connection.execute({ type: "turn.start", sessionId: "session-1", userInput: "hello" });
    const firstRequest = sockets[0].sent.find((message) => message.kind === "request");
    sockets[0].close();
    await new Promise((resolve) => setTimeout(resolve, 0));
    sockets[1].open();
    const retriedRequest = sockets[1].sent.find((message) => message.kind === "request");

    expect(retriedRequest?.request_id).toBe(firstRequest?.request_id);
    sockets[1].emit({
      kind: "response",
      request_id: retriedRequest?.request_id,
      ok: true,
      result: { turn_id: "turn-1", session_id: "session-1" },
    });
    await expect(pending).resolves.toEqual({ turn_id: "turn-1", session_id: "session-1" });
    connection.close();
  });

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

class RecordingRelaySocket {
  onopen: ((event: Event) => unknown) | null = null;
  onclose: ((event: CloseEvent) => unknown) | null = null;
  onerror: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent) => unknown) | null = null;
  sent: Array<Record<string, unknown>> = [];

  open() {
    this.onopen?.(new Event("open"));
  }

  send(rawMessage: string) {
    this.sent.push(JSON.parse(rawMessage) as Record<string, unknown>);
  }

  emit(message: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(message) }));
  }

  close() {
    this.onclose?.(new Event("close") as CloseEvent);
  }
}

function sequencedEvent(sequence: number, delta: string) {
  return {
    kind: "event",
    protocol_version: 1,
    device_id: "device-1",
    project_id: "project-1",
    stream_epoch: "epoch-1",
    sequence,
    event: { type: "assistant_delta", payload: { delta } },
  };
}
