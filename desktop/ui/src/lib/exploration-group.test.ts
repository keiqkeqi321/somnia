import { describe, expect, it } from "vitest";

import {
  groupExplorationParts,
  isExplorationGroup,
  isExplorationToolName,
  toolCallExcerpt,
  explorationGroupSummary,
} from "./exploration-group";
import type { ConversationRowPart, ConversationToolCall } from "../types";

function tool(name: string, status: "running" | "finished" = "finished", rawInput: unknown = {}): ConversationToolCall {
  return { id: `tc-${name}-${Math.random()}`, name, input: "", output: "", rawInput, status };
}

let seq = 0;
function toolPart(name: string, status: "running" | "finished" = "finished", rawInput: unknown = {}): ConversationRowPart {
  seq += 1;
  return { id: `p${seq}`, type: "tool_call", toolCall: tool(name, status, rawInput) };
}

function thinkingPart(status: "running" | "finished" = "finished"): ConversationRowPart {
  seq += 1;
  return { id: `p${seq}`, type: "thinking_log", thinkingLog: { status, text: "…" } };
}

function textPart(text: string): ConversationRowPart {
  seq += 1;
  return { id: `p${seq}`, type: "text", text };
}

describe("isExplorationToolName", () => {
  it("matches built-in read-only tools", () => {
    expect(isExplorationToolName("read_file")).toBe(true);
    expect(isExplorationToolName("grep")).toBe(true);
    expect(isExplorationToolName("tree")).toBe(true);
  });

  it("matches read-only MCP tools by name segment", () => {
    expect(isExplorationToolName("mcp_codegraph_codegraph_explore")).toBe(true);
    expect(isExplorationToolName("mcp_fs_read_file")).toBe(true);
  });

  it("rejects mutating tools", () => {
    expect(isExplorationToolName("bash")).toBe(false);
    expect(isExplorationToolName("write_file")).toBe(false);
    expect(isExplorationToolName("edit_file")).toBe(false);
    expect(isExplorationToolName("mcp_fs_write_file")).toBe(false);
    expect(isExplorationToolName("mcp_deploy_target")).toBe(false);
  });
});

describe("groupExplorationParts", () => {
  it("folds a run of exploration calls and thinking into one group", () => {
    const grouped = groupExplorationParts([toolPart("grep"), thinkingPart(), toolPart("read_file")]);
    expect(grouped).toHaveLength(1);
    const group = grouped[0];
    expect(isExplorationGroup(group)).toBe(true);
    if (isExplorationGroup(group)) {
      expect(group.parts).toHaveLength(3);
    }
  });

  it("keeps a running part outside the group", () => {
    const grouped = groupExplorationParts([toolPart("grep"), toolPart("read_file"), toolPart("grep", "running")]);
    expect(grouped).toHaveLength(2);
    expect(isExplorationGroup(grouped[0])).toBe(true);
    expect(isExplorationGroup(grouped[1])).toBe(false);
  });

  it("keeps a single collapsible part ungrouped", () => {
    const grouped = groupExplorationParts([textPart("a"), toolPart("grep"), textPart("b")]);
    expect(grouped.every((part) => !isExplorationGroup(part))).toBe(true);
  });

  it("splits groups around non-exploration tools and text", () => {
    const grouped = groupExplorationParts([
      toolPart("grep"),
      toolPart("read_file"),
      toolPart("bash"),
      toolPart("grep"),
      toolPart("glob"),
      textPart("done"),
    ]);
    const groups = grouped.filter(isExplorationGroup);
    expect(groups).toHaveLength(2);
    expect(grouped.filter((part) => !isExplorationGroup(part))).toHaveLength(2);
  });

  it("groups two finished thinking logs", () => {
    const grouped = groupExplorationParts([thinkingPart(), thinkingPart()]);
    expect(grouped).toHaveLength(1);
    expect(isExplorationGroup(grouped[0])).toBe(true);
  });
});

describe("explorationGroupSummary", () => {
  it("counts per kind, most frequent first", () => {
    const summary = explorationGroupSummary([
      toolPart("grep"),
      toolPart("grep"),
      toolPart("read_file"),
      thinkingPart(),
    ].flatMap((part) => [part]));
    expect(summary[0]).toMatchObject({ label: "grep", count: 2, thinking: false });
    expect(summary[1]).toMatchObject({ label: "read_file", count: 1 });
    expect(summary[2]).toMatchObject({ count: 1, thinking: true });
  });
});

describe("toolCallExcerpt", () => {
  it("prefers command, then query/pattern/path/url", () => {
    expect(toolCallExcerpt(tool("bash", "finished", { command: "npm  test" }))).toBe("npm test");
    expect(toolCallExcerpt(tool("grep", "finished", { pattern: "foo", path: "src" }))).toBe("foo");
    expect(toolCallExcerpt(tool("read_file", "finished", { path: "a/b.ts" }))).toBe("a/b.ts");
    expect(toolCallExcerpt(tool("web_fetch", "finished", { url: "https://x.dev" }))).toBe("https://x.dev");
  });

  it("returns empty string when nothing excerptable", () => {
    expect(toolCallExcerpt(tool("tree", "finished", { depth: 2 }))).toBe("");
    expect(toolCallExcerpt(tool("tree", "finished", null))).toBe("");
  });
});
