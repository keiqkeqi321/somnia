import type { AgentSession, ConversationRow, SessionMessage } from "../types";

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
    if (item.type === "tool_result" && typeof item.content === "string") {
      parts.push(item.content);
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
  if (typeof message.content !== "string") {
    return extractTextContent(message.content).trim().length > 0;
  }
  return !message.content.startsWith("<background-results>") && !message.content.startsWith("<inbox>");
}

export function buildConversationRows(session: AgentSession | null, streamingText: string): ConversationRow[] {
  if (!session) {
    return streamingText
      ? [
          {
            id: "streaming-only",
            role: "assistant",
            text: streamingText,
            isStreaming: true,
          },
        ]
      : [];
  }
  const rows: ConversationRow[] = [];
  for (let index = 0; index < session.messages.length; index += 1) {
    const message = session.messages[index];
    if (message.role !== "assistant" && !isVisibleUserMessage(message)) {
      continue;
    }
    const text = extractTextContent(message.content).trim();
    if (!text) {
      continue;
    }
    if (message.role === "assistant") {
      rows.push({ id: `${session.id}-assistant-${index}`, role: "assistant", text });
      continue;
    }
    rows.push({ id: `${session.id}-user-${index}`, role: "user", text });
  }
  if (streamingText.trim()) {
    rows.push({
      id: `${session.id}-streaming`,
      role: "assistant",
      text: streamingText,
      isStreaming: true,
    });
  }
  return rows;
}

export function buildSessionPreview(session: AgentSession): string {
  for (let index = session.messages.length - 1; index >= 0; index -= 1) {
    const message = session.messages[index];
    if (message.role === "assistant") {
      const text = extractTextContent(message.content).trim();
      if (text) {
        return compressWhitespace(text).slice(0, 84);
      }
    }
    if (isVisibleUserMessage(message)) {
      const text = extractTextContent(message.content).trim();
      if (text) {
        return compressWhitespace(text).slice(0, 84);
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
    return "just now";
  }
  const delta = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (delta < 45) {
    return "just now";
  }
  if (delta < 3600) {
    return `${Math.round(delta / 60)} min ago`;
  }
  if (delta < 86_400) {
    return `${Math.round(delta / 3600)} hr ago`;
  }
  return `${Math.round(delta / 86_400)} d ago`;
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
