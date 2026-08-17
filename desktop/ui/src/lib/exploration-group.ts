import type { ConversationRowPart, ConversationToolCall } from "../types";

/* Exploration grouping: long runs of read-only tool calls and thinking logs
 * collapse into one expandable row so the conversation keeps its shape. A
 * part that is still running always stays outside the group and drops into
 * it once it finishes. */

const EXPLORATION_TOOL_NAMES = new Set([
  "read_file",
  "read_image",
  "grep",
  "glob",
  "tree",
  "find_symbol",
  "web_fetch",
  "web_search",
  "task_get",
  "task_list",
  "check_background",
  "list_teammates",
  "read_inbox",
]);

// MCP tools are named mcp_<server>_<tool...>; segment-match the read-only
// verbs so e.g. mcp_codegraph_codegraph_explore folds in without whitelisting
// every server.
const EXPLORATION_SEGMENTS = new Set([
  "explore",
  "search",
  "read",
  "get",
  "list",
  "find",
  "query",
  "lookup",
  "grep",
  "glob",
  "tree",
  "symbol",
  "symbols",
  "fetch",
  "ls",
  "cat",
]);

export function isExplorationToolName(name: string): boolean {
  const normalized = name.trim().toLowerCase();
  if (!normalized) {
    return false;
  }
  if (EXPLORATION_TOOL_NAMES.has(normalized)) {
    return true;
  }
  if (normalized.startsWith("mcp_")) {
    return normalized
      .split("_")
      .slice(1)
      .some((segment) => EXPLORATION_SEGMENTS.has(segment));
  }
  return false;
}

export interface ExplorationGroupPart {
  id: string;
  type: "exploration_group";
  parts: ConversationRowPart[];
}

export type ConversationRenderPart = ConversationRowPart | ExplorationGroupPart;

export function isExplorationGroup(part: ConversationRenderPart): part is ExplorationGroupPart {
  return part.type === "exploration_group";
}

function isCollapsiblePart(part: ConversationRowPart): boolean {
  if (part.type === "thinking_log") {
    return part.thinkingLog.status !== "running";
  }
  if (part.type === "tool_call") {
    return part.toolCall.status !== "running" && isExplorationToolName(part.toolCall.name);
  }
  return false;
}

/** Fold maximal runs of >= 2 collapsible parts into one exploration group. */
export function groupExplorationParts(parts: ConversationRowPart[]): ConversationRenderPart[] {
  const out: ConversationRenderPart[] = [];
  let run: ConversationRowPart[] = [];
  const flush = () => {
    if (run.length >= 2) {
      out.push({ id: `explore-${run[0].id}`, type: "exploration_group", parts: run });
    } else {
      out.push(...run);
    }
    run = [];
  };
  for (const part of parts) {
    if (isCollapsiblePart(part)) {
      run.push(part);
    } else {
      flush();
      out.push(part);
    }
  }
  flush();
  return out;
}

/** Per-kind counts for the group header, most frequent first. */
export function explorationGroupSummary(parts: ConversationRowPart[]): Array<{ label: string; count: number; thinking: boolean }> {
  const counts = new Map<string, { label: string; count: number; thinking: boolean }>();
  for (const part of parts) {
    const key = part.type === "thinking_log" ? "\0thinking" : part.type === "tool_call" ? part.toolCall.name : "";
    if (!key) {
      continue;
    }
    const existing = counts.get(key);
    if (existing) {
      existing.count += 1;
    } else {
      counts.set(key, { label: part.type === "thinking_log" ? "" : key, count: 1, thinking: part.type === "thinking_log" });
    }
  }
  return [...counts.values()].sort((a, b) => b.count - a.count);
}

const EXCERPT_KEYS = ["command", "query", "pattern", "path", "url"] as const;

/** One-line excerpt of what a call actually looked at (command/pattern/path…). */
export function toolCallExcerpt(toolCall: ConversationToolCall): string {
  const input = toolCall.rawInput;
  if (input && typeof input === "object" && !Array.isArray(input)) {
    const record = input as Record<string, unknown>;
    for (const key of EXCERPT_KEYS) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) {
        return collapseExcerpt(value);
      }
    }
  }
  return "";
}

function collapseExcerpt(value: string): string {
  return value.trim().replace(/\s+/g, " ");
}
