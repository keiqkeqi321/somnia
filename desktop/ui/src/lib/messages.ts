import type {
  AgentSession,
  ConversationPendingTurn,
  ConversationRow,
  ConversationRuntimeItem,
  ConversationToolCall,
  SessionMessage,
} from "../types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function extractTextContent(content: unknown): string {
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  const parts: string[] = [];
  for (const item of content) {
    if (typeof item === "string") {
      parts.push(item);
      continue;
    }
    if (!isRecord(item)) {
      continue;
    }
    if (item.type === "text" && typeof item.text === "string") {
      parts.push(item.text);
      continue;
    }
    if (item.type === "tool_call" || item.type === "tool_result") {
      continue;
    }
    if (typeof item.content === "string") {
      parts.push(item.content);
    }
  }
  return parts.join("\n").trim();
}

function isVisibleUserMessage(message: SessionMessage): boolean {
  if (message.role !== "user") {
    return false;
  }
  if (hasToolResults(message.content)) {
    return false;
  }
  if (typeof message.content !== "string") {
    return extractTextContent(message.content).trim().length > 0;
  }
  return !message.content.startsWith("<background-results>") && !message.content.startsWith("<inbox>");
}

export function buildConversationRows(
  session: AgentSession | null,
  runtimeItems: ConversationRuntimeItem[],
  pendingTurn: ConversationPendingTurn | null = null,
): ConversationRow[] {
  const hasRuntimeItems = runtimeItems.some((item) =>
    item.type === "assistant_text" ? item.text.trim().length > 0 : true,
  );
  if (!session) {
    const rows: ConversationRow[] = [];
    appendPendingTurn(rows, pendingTurn, !hasRuntimeItems);
    appendRuntimeItems(rows, runtimeItems);
    return rows;
  }
  const rows: ConversationRow[] = [];
  let index = 0;
  while (index < session.messages.length) {
    const message = session.messages[index];
    if (message.role !== "assistant" && !isVisibleUserMessage(message)) {
      index += 1;
      continue;
    }
    const text = extractTextContent(message.content).trim();
    if (message.role === "assistant") {
      const toolCalls = buildToolCalls(message.content, session.messages[index + 1]?.content);
      if (toolCalls.length > 0 && session.messages[index + 1]?.role === "user" && hasToolResults(session.messages[index + 1]?.content)) {
        index += 1;
      }
      if (text || toolCalls.length > 0) {
        appendAssistantRow(rows, { id: `${session.id}-assistant-${index}`, role: "assistant", text, toolCalls });
      }
      index += 1;
      continue;
    }
    if (text) {
      rows.push({ id: `${session.id}-user-${index}`, role: "user", text });
    }
    index += 1;
  }
  const shouldShowPendingTurn = pendingTurn !== null && pendingTurn.sessionId === session.id;
  if (shouldShowPendingTurn) {
    appendPendingTurn(rows, pendingTurn, !hasRuntimeItems);
  }
  appendRuntimeItems(rows, runtimeItems);
  return rows;
}

function appendPendingTurn(rows: ConversationRow[], pendingTurn: ConversationPendingTurn | null, includePlaceholder: boolean) {
  if (!pendingTurn) {
    return;
  }
  if (pendingTurn.userText.trim()) {
    rows.push({
      id: `${pendingTurn.id}-user`,
      role: "user",
      text: pendingTurn.userText,
      isPending: true,
    });
  }
  if (includePlaceholder) {
    rows.push({
      id: `${pendingTurn.id}-assistant`,
      role: "assistant",
      text: pendingTurn.placeholderText,
      isLoading: true,
      isPending: true,
    });
  }
}

function appendRuntimeItems(rows: ConversationRow[], runtimeItems: ConversationRuntimeItem[]) {
  for (const item of runtimeItems) {
    if (item.type === "assistant_text") {
      if (!item.text.trim()) {
        continue;
      }
      appendAssistantRow(rows, {
        id: item.id,
        role: "assistant",
        text: item.text,
        isStreaming: true,
      });
      continue;
    }
    appendAssistantRow(rows, {
      id: item.id,
      role: "assistant",
      text: "",
      toolCalls: [item.toolCall],
      isStreaming: item.toolCall.status === "running",
    });
  }
}

function appendAssistantRow(rows: ConversationRow[], row: ConversationRow) {
  const last = rows[rows.length - 1];
  if (last?.role !== "assistant" || last.isPending || row.isPending) {
    rows.push(row);
    return;
  }
  rows[rows.length - 1] = {
    ...last,
    text: mergeAssistantText(last.text, row.text),
    toolCalls: [...(last.toolCalls ?? []), ...(row.toolCalls ?? [])],
    isStreaming: Boolean(last.isStreaming || row.isStreaming),
    isLoading: Boolean(last.isLoading || row.isLoading),
  };
}

function mergeAssistantText(left: string, right: string): string {
  if (!left.trim()) {
    return right;
  }
  if (!right.trim()) {
    return left;
  }
  return `${left.trimEnd()}\n\n${right.trimStart()}`;
}

function hasToolResults(content: unknown): boolean {
  return Array.isArray(content) && content.some((item) => isRecord(item) && item.type === "tool_result");
}

function buildToolCalls(assistantContent: unknown, nextUserContent: unknown): ConversationToolCall[] {
  if (!Array.isArray(assistantContent)) {
    return [];
  }
  const results = toolResultMap(nextUserContent);
  const calls: ConversationToolCall[] = [];
  for (const item of assistantContent) {
    if (!isRecord(item) || item.type !== "tool_call") {
      continue;
    }
    const id = String(item.id ?? "").trim();
    const result = id ? results.get(id) : undefined;
    calls.push({
      id: id || `tool-${calls.length + 1}`,
      name: String(item.name ?? "tool").trim() || "tool",
      input: stringifyToolValue(item.input ?? {}),
      output: stringifyToolValue(toolResultOutput(result)),
      logId: isRecord(result) && typeof result.log_id === "string" ? result.log_id : null,
    });
  }
  return calls;
}

function toolResultMap(content: unknown): Map<string, Record<string, unknown>> {
  const results = new Map<string, Record<string, unknown>>();
  if (!Array.isArray(content)) {
    return results;
  }
  for (const item of content) {
    if (!isRecord(item) || item.type !== "tool_result") {
      continue;
    }
    const id = String(item.tool_call_id ?? "").trim();
    if (id) {
      results.set(id, item);
    }
  }
  return results;
}

function toolResultOutput(result: Record<string, unknown> | undefined): unknown {
  if (!result) {
    return "(no output)";
  }
  return result.raw_output ?? result.content ?? "(no output)";
}

export function stringifyToolValue(value: unknown): string {
  if (typeof value === "string") {
    return value.trim() || "(empty)";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function buildSessionPreview(session: AgentSession): string {
  for (let index = session.messages.length - 1; index >= 0; index -= 1) {
    const message = session.messages[index];
    if (message.role === "assistant") {
      const text = extractTextContent(message.content).trim();
      if (text) {
        return compressWhitespace(text).slice(0, 56);
      }
    }
    if (isVisibleUserMessage(message)) {
      const text = extractTextContent(message.content).trim();
      if (text) {
        return compressWhitespace(text).slice(0, 56);
      }
    }
  }
  return "No visible history yet";
}

export function sortSessions(sessions: AgentSession[]): AgentSession[] {
  return [...sessions].sort((left, right) => {
    const leftStamp = left.updated_at ?? left.created_at ?? 0;
    const rightStamp = right.updated_at ?? right.created_at ?? 0;
    return rightStamp - leftStamp;
  });
}

export function formatRelativeTime(timestamp: number | null | undefined): string {
  if (!timestamp) {
    return "now";
  }
  const delta = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (delta < 45) {
    return "now";
  }
  if (delta < 3600) {
    return `${Math.round(delta / 60)}m`;
  }
  if (delta < 86_400) {
    return `${Math.round(delta / 3600)}h`;
  }
  return `${Math.round(delta / 86_400)}d`;
}

export function formatTodoLabel(item: { content?: string; status?: string; activeForm?: string }): string {
  const content = String(item.content ?? "").trim();
  if (!content) {
    return "";
  }
  if (String(item.status ?? "").trim().toLowerCase() === "in_progress") {
    const activeForm = String(item.activeForm ?? "").trim();
    return activeForm ? `${content} <- ${activeForm}` : content;
  }
  return content;
}

function compressWhitespace(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}
