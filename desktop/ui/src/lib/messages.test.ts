import { describe, expect, it } from "vitest";

import type { AgentSession, ConversationPendingTurn, ConversationRuntimeItem } from "../types";
import { buildConversationRows } from "./messages";

const session: AgentSession = {
  id: "session-1",
  messages: [{ role: "user", content: "Question" }],
  token_usage: {},
  todo_items: [],
  rounds_without_todo: 0,
};

describe("shared conversation rows", () => {
  it("builds rows for empty, streaming, completed, and interrupted conversations", () => {
    expect(buildConversationRows(null, [])).toEqual([]);

    const pending: ConversationPendingTurn = {
      id: "pending-1",
      sessionId: session.id,
      userText: "Question",
      placeholderText: "Working...",
    };
    const streaming: ConversationRuntimeItem[] = [
      { type: "assistant_text", id: "stream-1", text: "Working", isStreaming: true },
    ];
    expect(buildConversationRows(session, streaming, pending)).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: "assistant", text: "Working", isStreaming: true }),
    ]));

    const completed: AgentSession = {
      ...session,
      messages: [...session.messages, { role: "assistant", content: "Done" }],
    };
    expect(buildConversationRows(completed, [])).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: "assistant", text: "Done", isStreaming: false }),
    ]));

    const interrupted: ConversationRuntimeItem[] = [
      { type: "assistant_text", id: "stream-2", text: "Partial", isStreaming: false },
    ];
    expect(buildConversationRows(session, interrupted)).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: "assistant", text: "Partial", isStreaming: false }),
    ]));
  });

  it("replaces an interrupted runtime row with the resynchronized session row", () => {
    const interrupted: ConversationRuntimeItem[] = [
      { type: "assistant_text", id: "stream-2", text: "Partial", isStreaming: false },
    ];
    const resynchronized: AgentSession = {
      ...session,
      messages: [...session.messages, { role: "assistant", content: "Recovered" }],
    };

    expect(buildConversationRows(session, interrupted)).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "stream-2", text: "Partial", isStreaming: false }),
    ]));
    expect(buildConversationRows(resynchronized, [])).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "session-1-assistant-1", text: "Recovered", isStreaming: false }),
    ]));
    expect(buildConversationRows(resynchronized, []).map((row) => row.id)).not.toContain("stream-2");
  });
});
