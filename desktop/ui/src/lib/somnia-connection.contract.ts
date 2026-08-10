import { describe, expect, it } from "vitest";

import type {
  AgentSession,
  McpServerSummary,
  ProviderPresetDescriptor,
  SaveSettingsConfigSectionResult,
  SettingsConfigPayload,
  SidecarEvent,
  SidecarStatus,
  TaskGraphItem,
  ToolLogDetail,
  ToolLogIndexEntry,
  WorkspacePathSuggestion,
} from "../types";
import type { SomniaClient } from "./somnia-client";
import type { SomniaConnectionNotification } from "./somnia-connection";

export interface SomniaConnectionContractHarness {
  connection: SomniaClient;
  openStream(): void;
  emitEvent(event: SidecarEvent): void;
  closeStream(): void;
}

export const contractRuntimeStatus: SidecarStatus = {
  status: "ready",
  version: "0.5.2",
  workspace_root: "C:/workspace",
  base_url: "http://127.0.0.1:8765",
  ws_url: "ws://127.0.0.1:8765/ws",
  provider: "openai",
  model: "gpt-test",
};

export const contractWorkspacePaths: WorkspacePathSuggestion[] = [{ path: "src", basename: "src", kind: "dir" }];

export const contractTasks: TaskGraphItem[] = [{ id: 1, subject: "Write tests", status: "pending" }];

export const contractSettingsConfig: SettingsConfigPayload = {
  scopes: [
    {
      scope: "project",
      label: "Project",
      config_path: "C:/workspace/open_somnia.toml",
      config_exists: true,
      skills_path: "C:/workspace/skills",
      skills_exists: false,
      sections: {
        provider: "[providers.openai]",
        mcp: "",
        hooks: "[hooks]",
        system_prompt: "",
        runtime: "",
      },
      skills: [],
    },
  ],
};

export const contractSettingsSaveResult: SaveSettingsConfigSectionResult = {
  scope: "project",
  section: "hooks",
  config_path: "C:/workspace/open_somnia.toml",
  saved: true,
  restart_required: false,
  runtime_reloaded: true,
};

export const contractProviderPresets: ProviderPresetDescriptor[] = [
  {
    id: "openai",
    label: "OpenAI",
    provider_name: "openai",
    provider_type: "openai",
    base_url: "https://api.openai.com/v1",
    models: ["gpt-test"],
    default_model: "gpt-test",
  },
];

export const contractMcpServers: McpServerSummary[] = [
  {
    name: "docs",
    transport: "stdio",
    target: "docs-server",
    enabled: true,
    status: "connected",
    tool_count: 2,
    enabled_tool_count: 1,
    tools: [
      { name: "search", description: "Search the docs.", input_schema: {}, enabled: true },
      { name: "fetch", description: "Fetch a doc page.", input_schema: {}, enabled: false },
    ],
  },
];

export const contractSetMcpToolEnabledResult: { server: McpServerSummary; tool: string; enabled: boolean; config_path: string } = {
  server: contractMcpServers[0],
  tool: "fetch",
  enabled: true,
  config_path: "C:/workspace/open_somnia.toml",
};

export const contractToolLogs: ToolLogIndexEntry[] = [
  { id: "log-1", timestamp: 1700000000, actor: "agent", tool_name: "bash", category: "shell", path: "tool-logs/log-1.json" },
];

export const contractToolLogDetail: ToolLogDetail = {
  ...contractToolLogs[0],
  tool_input: { command: "ls" },
  output: "ok",
  rendered: "bash: ls",
};

export function describeSomniaConnectionContract(
  name: string,
  createHarness: () => SomniaConnectionContractHarness,
  loadedSession: AgentSession,
) {
  describe(`${name} Somnia Connection contract`, () => {
    it("loads a Session and starts a Turn through the shared interface", async () => {
      const harness = createHarness();
      const { connection } = harness;
      connection.subscribe(() => undefined);
      harness.openStream();

      await expect(connection.execute({ type: "session.create" })).resolves.toEqual(loadedSession);
      await expect(connection.query({ type: "session.list" })).resolves.toEqual([loadedSession]);
      await expect(connection.query({ type: "session.load", sessionId: loadedSession.id })).resolves.toEqual(loadedSession);
      await expect(connection.execute({ type: "session.delete", sessionId: loadedSession.id })).resolves.toEqual({ session_id: loadedSession.id, deleted: true });
      await expect(
        connection.execute({ type: "turn.start", sessionId: loadedSession.id, userInput: "Continue" }),
      ).resolves.toEqual({ turn_id: "turn-1", session_id: loadedSession.id });
    });

    it("publishes connection state and Runtime events in order", () => {
      const harness = createHarness();
      const notifications: SomniaConnectionNotification[] = [];
      const unsubscribe = harness.connection.subscribe((notification) => notifications.push(notification));
      const event: SidecarEvent = {
        type: "assistant_delta",
        session_id: loadedSession.id,
        turn_id: "turn-1",
        payload: { delta: "Hello" },
      };

      harness.openStream();
      harness.emitEvent(event);
      harness.closeStream();
      unsubscribe();

      expect(notifications).toEqual([
        { kind: "state", state: "connecting" },
        { kind: "state", state: "connected" },
        { kind: "event", event },
        { kind: "state", state: "disconnected" },
      ]);
      expect(harness.connection.connectionState()).toBe("disconnected");
    });

    it("exposes runtime status, workspace paths, and tasks through the shared interface", async () => {
      const harness = createHarness();
      const { connection } = harness;
      connection.subscribe(() => undefined);
      harness.openStream();

      await expect(connection.runtimeStatus()).resolves.toEqual(contractRuntimeStatus);
      await expect(connection.listWorkspacePaths("src", 30)).resolves.toEqual(contractWorkspacePaths);
      await expect(connection.listTasks()).resolves.toEqual(contractTasks);
    });

    it("reads and writes settings config and lists provider presets and MCP servers", async () => {
      const harness = createHarness();
      const { connection } = harness;
      connection.subscribe(() => undefined);
      harness.openStream();

      await expect(connection.getSettingsConfig()).resolves.toEqual(contractSettingsConfig);
      await expect(connection.saveSettingsConfigSection("project", "hooks", "[hooks]")).resolves.toEqual(contractSettingsSaveResult);
      await expect(connection.listProviderPresets()).resolves.toEqual(contractProviderPresets);
      await expect(connection.listMcpServers()).resolves.toEqual(contractMcpServers);
      await expect(connection.setMcpToolEnabled("docs", "fetch", true)).resolves.toEqual(contractSetMcpToolEnabledResult);
    });

    it("resolves authorization and question interactions and reads tool logs", async () => {
      const harness = createHarness();
      const { connection } = harness;
      connection.subscribe(() => undefined);
      harness.openStream();

      await expect(
        connection.resolveAuthorization("interaction-1", { scope: "workspace", approved: true, reason: "ok" }),
      ).resolves.toBeUndefined();
      await expect(
        connection.resolveQuestion("interaction-2", { answer: "Blue", selectedOption: "Blue", status: "answered", reason: "" }),
      ).resolves.toBeUndefined();
      await expect(connection.listToolLogs(24)).resolves.toEqual(contractToolLogs);
      await expect(connection.getToolLog("log-1")).resolves.toEqual(contractToolLogDetail);
    });
  });
}
