import type {
  AgentSession,
  ConversationContentBlock,
  ConversationImageReferenceBlock,
  ConversationPendingTurn,
  ConversationRow,
  ConversationRowPart,
  ConversationRuntimeItem,
  ConversationToolCall,
  SessionMessage,
} from "../types";

const SHORT_DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
});

const SHORT_DATE_WITH_YEAR_FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});

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
    const text = extractTextContent(message.content).trim();
    if (text.length > 0) {
      return true;
    }
    return extractUserImages(message.content).length > 0;
  }
  return !message.content.startsWith("<background-results>") && !message.content.startsWith("<inbox>");
}

function extractUserImages(content: unknown): ConversationImageReferenceBlock[] {
  if (!Array.isArray(content)) {
    return [];
  }
  const blocks: ConversationImageReferenceBlock[] = [];
  for (const item of content) {
    if (!isRecord(item)) {
      continue;
    }
    const ref = userImageBlock(item);
    if (ref) {
      blocks.push(ref);
    }
  }
  return blocks;
}

function userImageBlock(item: Record<string, unknown>): ConversationImageReferenceBlock | null {
  if (item.type === "input_image") {
    const path = typeof item.path === "string" ? item.path : undefined;
    const absolutePath = typeof item.absolute_path === "string" ? item.absolute_path : undefined;
    const mediaType = typeof item.media_type === "string" ? item.media_type : undefined;
    if (!path && !absolutePath) {
      return null;
    }
    return {
      type: "image_reference",
      path,
      absolute_path: absolutePath,
      media_type: mediaType,
      origin: "user_input",
    };
  }
  const generic = toolImageReferenceBlock(item);
  if (generic && generic.type === "image_reference") {
    return generic;
  }
  return null;
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
      const rowId = `${session.id}-assistant-${index}`;
      const parts = buildAssistantParts(rowId, message.content, session.messages[index + 1]?.content);
      const toolCalls = parts.flatMap((part) => (part.type === "tool_call" ? [part.toolCall] : []));
      if (toolCalls.length > 0 && session.messages[index + 1]?.role === "user" && hasToolResults(session.messages[index + 1]?.content)) {
        index += 1;
      }
      if (parts.length > 0) {
        appendAssistantRow(rows, { id: rowId, role: "assistant", text, parts, toolCalls });
      }
      index += 1;
      continue;
    }
    const images = extractUserImages(message.content);
    if (text || images.length > 0) {
      rows.push({ id: `${session.id}-user-${index}`, role: "user", text: text || "", ...(images.length > 0 ? { images } : {}) });
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
    if (item.type === "user_text") {
      if (!item.text.trim()) {
        continue;
      }
      rows.push({
        id: item.id,
        role: "user",
        text: item.text,
      });
      continue;
    }
    if (item.type === "assistant_text") {
      if (!item.text.trim()) {
        continue;
      }
      appendAssistantRow(rows, {
        id: item.id,
        role: "assistant",
        text: item.text,
        parts: [{ id: `${item.id}-text`, type: "text", text: item.text }],
        isStreaming: item.isStreaming ?? true,
      });
      continue;
    }
    appendAssistantRow(rows, {
      id: item.id,
      role: "assistant",
      text: "",
      parts: [{ id: item.toolCall.id, type: "tool_call", toolCall: item.toolCall }],
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
    parts: [...rowParts(last), ...rowParts(row)],
    toolCalls: [...(last.toolCalls ?? []), ...(row.toolCalls ?? [])],
    isStreaming: Boolean(last.isStreaming || row.isStreaming),
    isLoading: Boolean(last.isLoading || row.isLoading),
  };
}

function rowParts(row: ConversationRow): ConversationRowPart[] {
  if (row.parts) {
    return row.parts;
  }
  const parts: ConversationRowPart[] = [];
  if (row.text.trim()) {
    parts.push({ id: `${row.id}-text`, type: "text", text: row.text });
  }
  for (const toolCall of row.toolCalls ?? []) {
    parts.push({ id: toolCall.id, type: "tool_call", toolCall });
  }
  return parts;
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

function buildAssistantParts(rowId: string, assistantContent: unknown, nextUserContent: unknown): ConversationRowPart[] {
  if (typeof assistantContent === "string") {
    const text = assistantContent.trim();
    return text ? [{ id: `${rowId}-text-1`, type: "text", text }] : [];
  }
  if (!Array.isArray(assistantContent)) {
    return [];
  }
  const results = toolResultMap(nextUserContent);
  const parts: ConversationRowPart[] = [];
  let textCount = 0;
  let toolCount = 0;
  for (const item of assistantContent) {
    if (typeof item === "string") {
      const text = item.trim();
      if (text) {
        textCount += 1;
        parts.push({ id: `${rowId}-text-${textCount}`, type: "text", text });
      }
      continue;
    }
    if (!isRecord(item)) {
      continue;
    }
    const text = assistantPartText(item).trim();
    if (text) {
      textCount += 1;
      parts.push({ id: `${rowId}-text-${textCount}`, type: "text", text });
      continue;
    }
    if (item.type !== "tool_call") {
      continue;
    }
    toolCount += 1;
    const id = String(item.id ?? "").trim();
    const result = id ? results.get(id) : undefined;
    const toolCall = {
      id: id || `tool-${toolCount}`,
      name: String(item.name ?? "tool").trim() || "tool",
      input: stringifyToolValue(item.input ?? {}),
      output: stringifyToolValue(toolResultOutput(result)),
      rawInput: item.input ?? {},
      rawOutput: toolResultOutput(result),
      contentBlocks: toolResultContentBlocks(result),
      logId: isRecord(result) && typeof result.log_id === "string" ? result.log_id : null,
    };
    parts.push({ id: `${rowId}-${toolCall.id || `tool-${toolCount}`}`, type: "tool_call", toolCall });
  }
  return parts;
}

function assistantPartText(item: Record<string, unknown>): string {
  if (item.type === "text" && typeof item.text === "string") {
    return item.text;
  }
  if (typeof item.content === "string") {
    return item.content;
  }
  return "";
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

function toolResultContentBlocks(result: Record<string, unknown> | undefined): ConversationContentBlock[] {
  if (!result) {
    return [];
  }
  const contents: unknown[] = [result.content_blocks];
  if (isRecord(result.raw_output)) {
    contents.push(result.raw_output.content_blocks, result.raw_output.tool_result_content);
  }
  return normalizeToolContentBlocks(...contents);
}

export function normalizeToolContentBlocks(...contents: unknown[]): ConversationContentBlock[] {
  const blocks: ConversationContentBlock[] = [];
  for (const content of contents) {
    if (!Array.isArray(content)) {
      continue;
    }
    for (const item of content) {
      if (!isRecord(item)) {
        continue;
      }
      if (item.type === "text") {
        blocks.push({ type: "text", text: String(item.text ?? "") });
        continue;
      }
      const imageReference = toolImageReferenceBlock(item);
      if (imageReference) {
        blocks.push(imageReference);
      }
    }
  }
  return blocks;
}

function toolImageReferenceBlock(item: Record<string, unknown>): ConversationContentBlock | null {
  if (item.type === "image_reference") {
    const path = typeof item.path === "string" ? item.path : undefined;
    const absolutePath = typeof item.absolute_path === "string" ? item.absolute_path : undefined;
    const imageUrl = typeof item.image_url === "string" ? item.image_url : undefined;
    if (!path && !absolutePath && !imageUrl) {
      return null;
    }
    return {
      type: "image_reference",
      path,
      absolute_path: absolutePath,
      media_type: typeof item.media_type === "string" ? item.media_type : undefined,
      image_url: imageUrl,
      origin: typeof item.origin === "string" ? item.origin : undefined,
    };
  }
  if (item.type === "image_url") {
    const imageUrl = isRecord(item.image_url) ? item.image_url.url : item.image_url;
    if (typeof imageUrl !== "string" || !imageUrl.trim()) {
      return null;
    }
    return {
      type: "image_reference",
      media_type: mediaTypeFromDataUrl(imageUrl),
      image_url: imageUrl,
      origin: "tool_result",
    };
  }
  if (item.type === "image" && isRecord(item.source)) {
    const data = typeof item.source.data === "string" ? item.source.data : "";
    const mediaType = typeof item.source.media_type === "string" ? item.source.media_type : "image/png";
    if (!data.trim()) {
      return null;
    }
    return {
      type: "image_reference",
      media_type: mediaType,
      image_url: `data:${mediaType};base64,${data}`,
      origin: "tool_result",
    };
  }
  return null;
}

function mediaTypeFromDataUrl(value: string): string | undefined {
  const match = /^data:([^;,]+)[;,]/i.exec(value.trim());
  return match?.[1];
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
  if (typeof session.preview === "string" && session.preview.trim()) {
    return session.preview.trim();
  }
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
  const date = new Date(timestamp * 1000);
  const now = new Date();
  if (date.getFullYear() === now.getFullYear()) {
    return SHORT_DATE_FORMATTER.format(date);
  }
  return SHORT_DATE_WITH_YEAR_FORMATTER.format(date);
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
