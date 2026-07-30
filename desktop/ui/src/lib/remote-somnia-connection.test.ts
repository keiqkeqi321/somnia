import { describe, expect, it, vi } from "vitest";

import type { AgentSession, SidecarEvent } from "../types";
import {
  contractMcpServers,
  contractProviderPresets,
  contractRuntimeStatus,
  contractSettingsConfig,
  contractSettingsSaveResult,
  contractTasks,
  contractToolLogDetail,
  contractToolLogs,
  contractWorkspacePaths,
  describeSomniaConnectionContract,
} from "./somnia-connection.contract";
import { RemoteSomniaConnection } from "./remote-somnia-connection";
import type { SomniaConnectionNotification } from "./somnia-connection";

const loadedSession: AgentSession = {
  id: "session-1",
  messages: [{ role: "user", content: "Question" }],
  token_usage: {},
  todo_items: [],
  rounds_without_todo: 0,
};

function relayResultFor(method: string): unknown {
  switch (method) {
    case "turn.start":
      return { turn_id: "turn-1", session_id: loadedSession.id };
    case "session.list":
      return { sessions: [loadedSession] };
    case "session.delete":
      return { session_id: loadedSession.id, deleted: true };
    case "session.compact":
      return { message: "Context compacted.", session: loadedSession };
    case "session.janitor":
      return { message: "Janitor complete.", session: loadedSession };
    case "workspace.paths":
      return { paths: contractWorkspacePaths };
    case "workspace.image.stage":
      return { path: ".open_somnia/clipboard-images/paste.png", absolute_path: "C:/workspace/paste.png", media_type: "image/png" };
    case "runtime.status":
      return contractRuntimeStatus;
    case "provider.list":
      return { providers: [{ name: "openai", provider_type: "openai", default_model: "gpt-test", models: ["gpt-test"], is_active: true }] };
    case "model.list":
      return { models: [{ provider_name: "openai", name: "gpt-test", is_default: true, is_active: true, is_vision: false }] };
    case "interaction.list":
      return { interactions: [] };
    case "execution.mode":
      return { message: "Execution mode set.", execution_mode: "plan", execution_mode_title: "Plan mode" };
    case "settings.config.get":
      return contractSettingsConfig;
    case "settings.config.save":
      return contractSettingsSaveResult;
    case "provider.presets":
      return { presets: contractProviderPresets };
    case "mcp.list":
      return { servers: contractMcpServers };
    case "interaction.resolve_authorization":
      return { resolved: true };
    case "tool_log.list":
      return { tool_logs: contractToolLogs };
    case "tool_log.get":
      return contractToolLogDetail;
    case "task.list":
      return { tasks: contractTasks };
    default:
      return loadedSession;
  }
}

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
    this.emit({ kind: "response", request_id: request.request_id, ok: true, result: relayResultFor(request.method) });
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
  it("maps composer maintenance, path, and image operations to remote requests", async () => {
    const { connection, openStream } = createHarness();
    connection.subscribe(() => undefined);
    openStream();

    await expect(connection.compactSession("session-1")).resolves.toMatchObject({ message: "Context compacted." });
    await expect(connection.janitorSession("session-1")).resolves.toMatchObject({ message: "Janitor complete." });
    await expect(connection.listWorkspacePaths("src", 30)).resolves.toEqual([{ path: "src", basename: "src", kind: "dir" }]);
    await expect(connection.stageInlineImage({ name: "paste.png", mediaType: "image/png", dataUrl: "data:image/png;base64,cG5n" })).resolves.toMatchObject({ media_type: "image/png" });
    connection.close();
  });

  it("exposes approved model controls and interaction observation", async () => {
    const { connection, openStream } = createHarness();
    connection.subscribe(() => undefined);
    openStream();

    await expect(connection.runtimeStatus()).resolves.toMatchObject({ status: "ready" });
    await expect(connection.listProviders()).resolves.toHaveLength(1);
    await expect(connection.listModels("openai")).resolves.toHaveLength(1);
    await expect(connection.switchProviderModel("openai", "gpt-test")).resolves.toBeDefined();
    await expect(connection.setVisionModel("openai", "vision-test")).resolves.toBeDefined();
    await expect(connection.setReasoningLevel("high")).resolves.toBeDefined();
    await expect(connection.listInteractions()).resolves.toEqual([]);
    await expect(connection.setExecutionMode("plan")).resolves.toMatchObject({ execution_mode: "plan" });
    connection.close();
  });

  it("delivers turn_result even when the authoritative Session reload fails", async () => {
    class FailingLoadSocket extends FakeRelaySocket {
      send(rawMessage: string) {
        const request = JSON.parse(rawMessage) as { request_id?: string; method?: string };
        if (request.method === "session.load") {
          this.emit({ kind: "response", request_id: request.request_id, ok: false, error: "relay exploded" });
          return;
        }
        super.send(rawMessage);
      }
    }
    const socket = new FailingLoadSocket();
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      socketFactory: () => socket,
    });
    const notifications: Array<{ kind: string; event?: { type: string } }> = [];
    connection.subscribe((notification) => notifications.push(notification as { kind: string }));
    socket.open();
    socket.emit({
      kind: "event",
      project_id: "project-1",
      stream_epoch: "epoch-1",
      sequence: 1,
      event: { type: "turn_result", session_id: "session-1", turn_id: "turn-1", payload: {} },
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    const published = notifications.filter((notification) => notification.kind === "event");
    expect(published).toHaveLength(1);
    expect(published[0].event?.type).toBe("turn_result");
    connection.close();
  });

  it("holds requests made during a reconnect window until the socket returns", async () => {
    const { connection, openStream } = createHarness();
    // Issued while disconnected: must not reject immediately.
    const pending = connection.runtimeStatus();
    openStream();
    await expect(pending).resolves.toMatchObject({ status: "ready" });
    connection.close();
  });

  it("maps settings, provider, mcp, and interaction operations to remote requests", async () => {
    const socket = new RecordingRelaySocket();
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      socketFactory: () => socket,
    });
    connection.subscribe(() => undefined);
    socket.open();

    const pending = [
      connection.getSettingsConfig(),
      connection.saveSettingsConfigSection("project", "hooks", "[hooks]"),
      connection.listProviderPresets(),
      connection.debugModelConnection("openai", "gpt-test"),
      connection.listMcpServers(),
      connection.debugMcpServer("docs"),
      connection.setMcpServerEnabled("docs", false),
      connection.resolveAuthorization("interaction-1", { scope: "workspace", approved: true, reason: "ok" }),
      connection.resolveModeSwitch("interaction-2", { approved: true, activeMode: "yolo", reason: "" }),
      connection.setExecutionMode("yolo"),
      connection.setVisionModel("openai", "vision-test", "user"),
      connection.listTasks(),
      connection.getTeamLog("Scout"),
      connection.listActiveTeamMembers(),
    ];
    const requests = socket.sent.filter((message) => message.kind === "request");
    expect(requests.map((message) => [message.method, message.params])).toEqual([
      ["settings.config.get", {}],
      ["settings.config.save", { scope: "project", section: "hooks", content: "[hooks]" }],
      ["provider.presets", {}],
      ["provider.debug_model", { provider: "openai", model: "gpt-test" }],
      ["mcp.list", {}],
      ["mcp.debug", { name: "docs" }],
      ["mcp.set_enabled", { name: "docs", enabled: false }],
      ["interaction.resolve_authorization", { interaction_id: "interaction-1", scope: "workspace", approved: true, reason: "ok" }],
      ["interaction.resolve_mode_switch", { interaction_id: "interaction-2", approved: true, active_mode: "yolo", reason: "" }],
      ["execution.mode", { mode: "yolo" }],
      ["vision.set", { provider: "openai", model: "vision-test", scope: "user" }],
      ["task.list", {}],
      ["team.log", { name: "Scout" }],
      ["team.members", {}],
    ]);
    for (const request of requests) {
      socket.emit({
        kind: "response",
        request_id: request.request_id,
        ok: true,
        result: { presets: [], servers: [], tasks: [], members: [] },
      });
    }
    await expect(Promise.all(pending)).resolves.toBeDefined();
    connection.close();
  });

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

  it("stops reconnecting and surfaces an error when the Relay rejects authentication", async () => {
    const sockets: RecordingRelaySocket[] = [];
    const notifications: SomniaConnectionNotification[] = [];
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      reconnectDelayMs: 1,
      socketFactory: () => {
        const socket = new RecordingRelaySocket();
        sockets.push(socket);
        return socket;
      },
    });
    connection.subscribe((notification) => notifications.push(notification));
    sockets[0].open();

    sockets[0].onclose?.(
      Object.assign(new Event("close"), { code: 4401, reason: "Browser authentication required." }) as CloseEvent,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(sockets).toHaveLength(1);
    expect(notifications.some((notification) => notification.kind === "state" && notification.state === "error")).toBe(true);
    connection.close();
  });

  it("renews credentials via reauthorize and reconnects on an auth close", async () => {
    const sockets: RecordingRelaySocket[] = [];
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      reconnectDelayMs: 0,
      reauthorize: () => Promise.resolve(true),
      socketFactory: () => {
        const socket = new RecordingRelaySocket();
        sockets.push(socket);
        return socket;
      },
    });
    connection.subscribe(() => undefined);
    sockets[0].open();

    sockets[0].onclose?.(Object.assign(new Event("close"), { code: 4401, reason: "Browser authentication expired." }) as CloseEvent);
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(sockets).toHaveLength(2);
    connection.close();
  });

  it("surfaces an error without reconnecting when reauthorize cannot renew", async () => {
    const sockets: RecordingRelaySocket[] = [];
    const notifications: SomniaConnectionNotification[] = [];
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      reconnectDelayMs: 1,
      reauthorize: () => Promise.resolve(false),
      socketFactory: () => {
        const socket = new RecordingRelaySocket();
        sockets.push(socket);
        return socket;
      },
    });
    connection.subscribe((notification) => notifications.push(notification));
    sockets[0].open();

    sockets[0].onclose?.(Object.assign(new Event("close"), { code: 4401, reason: "Browser authentication expired." }) as CloseEvent);
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(sockets).toHaveLength(1);
    expect(notifications.some((notification) => notification.kind === "state" && notification.state === "error")).toBe(true);
    connection.close();
  });

  it("backs off exponentially between reconnect attempts", async () => {
    vi.useFakeTimers();
    try {
      const sockets: RecordingRelaySocket[] = [];
      const connection = new RemoteSomniaConnection({
        relayUrl: "ws://relay.test",
        deviceId: "device-1",
        projectId: "project-1",
        reconnectDelayMs: 100,
        socketFactory: () => {
          const socket = new RecordingRelaySocket();
          sockets.push(socket);
          return socket;
        },
      });
      connection.subscribe(() => undefined);
      expect(sockets).toHaveLength(1);

      sockets[0].close();
      await vi.advanceTimersByTimeAsync(99);
      expect(sockets).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(sockets).toHaveLength(2);

      sockets[1].close();
      await vi.advanceTimersByTimeAsync(199);
      expect(sockets).toHaveLength(2);
      await vi.advanceTimersByTimeAsync(1);
      expect(sockets).toHaveLength(3);
      connection.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("publishes stream snapshots and acknowledges the snapshot sequence", () => {
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
      kind: "stream_snapshot",
      protocol_version: 1,
      device_id: "device-1",
      project_id: "project-1",
      stream_epoch: "epoch-2",
      sequence: 7,
      snapshot: { sessions: [{ id: "session-1" }] },
    });
    // Events at or before the snapshot sequence are stale and must be dropped.
    socket.emit({
      kind: "event",
      protocol_version: 1,
      device_id: "device-1",
      project_id: "project-1",
      stream_epoch: "epoch-2",
      sequence: 6,
      event: { type: "assistant_delta", payload: { delta: "stale" } },
    });

    expect(notifications.filter((notification) => notification.kind === "snapshot")).toEqual([
      { kind: "snapshot", snapshot: { sessions: [{ id: "session-1" }] } },
    ]);
    expect(notifications.some((notification) => notification.kind === "event")).toBe(false);
    const acknowledgements = socket.sent.filter((message) => message.kind === "stream_ack");
    expect(acknowledgements[acknowledgements.length - 1]).toMatchObject({
      stream_epoch: "epoch-2",
      sequence: 7,
    });
    connection.close();
  });

  it("retries an unanswered stream_resume until a replay or snapshot arrives", async () => {
    vi.useFakeTimers();
    try {
      const socket = new RecordingRelaySocket();
      const connection = new RemoteSomniaConnection({
        relayUrl: "ws://relay.test",
        deviceId: "device-1",
        projectId: "project-1",
        socketFactory: () => socket,
      });
      connection.subscribe(() => undefined);
      socket.open();

      const resumeCount = () => socket.sent.filter((message) => message.kind === "stream_resume").length;
      expect(resumeCount()).toBe(1);
      await vi.advanceTimersByTimeAsync(2500);
      expect(resumeCount()).toBe(2);
      await vi.advanceTimersByTimeAsync(2500);
      expect(resumeCount()).toBe(3);

      socket.emit({
        kind: "stream_replay",
        protocol_version: 1,
        device_id: "device-1",
        project_id: "project-1",
        stream_epoch: "epoch-1",
        events: [],
      });
      await vi.advanceTimersByTimeAsync(10000);
      expect(resumeCount()).toBe(3);
      connection.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries stream_resume when the Relay reports an undeliverable frame", () => {
    const socket = new RecordingRelaySocket();
    const connection = new RemoteSomniaConnection({
      relayUrl: "ws://relay.test",
      deviceId: "device-1",
      projectId: "project-1",
      socketFactory: () => socket,
    });
    connection.subscribe(() => undefined);
    socket.open();

    const resumeCount = () => socket.sent.filter((message) => message.kind === "stream_resume").length;
    expect(resumeCount()).toBe(1);
    socket.emit({ kind: "response", request_id: "", ok: false, error: "Device connection failed." });
    expect(resumeCount()).toBe(2);
    connection.close();
  });

  it("publishes Turn completion immediately and enriches the Session afterwards", async () => {
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
        payload: { session: { ...loadedSession, messages: [] } },
      },
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    const enrichment = notifications.find(
      (notification) => notification.kind === "event" && notification.event.type === "session_updated",
    );
    expect(enrichment).toEqual({
      kind: "event",
      event: {
        type: "session_updated",
        session_id: loadedSession.id,
        turn_id: "turn-1",
        payload: { session: loadedSession },
      },
    });
  });

  it("publishes Turn completion even when the Session reload never responds", async () => {
    class HangingLoadSocket extends FakeRelaySocket {
      send(rawMessage: string) {
        const request = JSON.parse(rawMessage) as { request_id?: string; method?: string };
        if (request.method === "session.load") {
          // Never respond: the reload hangs without rejecting.
          return;
        }
        super.send(rawMessage);
      }
    }
    const socket = new HangingLoadSocket();
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
      kind: "event",
      project_id: "project-1",
      stream_epoch: "epoch-1",
      sequence: 1,
      event: { type: "turn_result", session_id: "session-1", turn_id: "turn-1", payload: {} },
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    const published = notifications.filter((notification) => notification.kind === "event");
    expect(published).toHaveLength(1);
    expect(published[0].event?.type).toBe("turn_result");
    connection.close();
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
