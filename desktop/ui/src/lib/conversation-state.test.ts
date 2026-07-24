import { describe, expect, it } from "vitest";

import type { AgentSession, SidecarEvent } from "../types";
import { createConversationState, transitionConversationEvent } from "./conversation-state";

const session: AgentSession = {
  id: "session-1",
  messages: [{ role: "user", content: "Question" }],
  token_usage: {},
  todo_items: [],
  rounds_without_todo: 0,
};

describe("conversation state", () => {
  it("leaves state unchanged for events outside the shared Turn path", () => {
    const initial = createConversationState(session);

    const transition = transitionConversationEvent(initial, {
      type: "session_updated",
      session_id: session.id,
      payload: { session: { ...session, updated_at: 2 } },
    });

    expect(transition).toEqual({ state: initial, effect: { type: "none" } });
  });

  it("deterministically reduces a streamed Turn without mutating prior state", () => {
    const completedSession: AgentSession = {
      ...session,
      messages: [...session.messages, { role: "assistant", content: "Hello world" }],
    };
    const events: SidecarEvent[] = [
      { type: "turn_started", session_id: session.id, turn_id: "turn-1", payload: {} },
      { type: "assistant_delta", session_id: session.id, turn_id: "turn-1", payload: { delta: "Hello " } },
      { type: "assistant_delta", session_id: session.id, turn_id: "turn-1", payload: { delta: "world" } },
      { type: "turn_result", session_id: session.id, turn_id: "turn-1", payload: { session: completedSession } },
    ];
    const initial = createConversationState(session);

    const transitions = [];
    let final = initial;
    for (const event of events) {
      const transition = transitionConversationEvent(final, event);
      transitions.push(transition.effect);
      final = transition.state;
    }

    expect(initial).toMatchObject({ sessionId: session.id, session, activeTurnId: null, assistantText: "" });
    expect(final).toMatchObject({
      sessionId: session.id,
      session: completedSession,
      activeTurnId: null,
      assistantText: "",
    });
    expect(transitions).toEqual([
      { type: "turn_started" },
      { type: "assistant_delta" },
      { type: "assistant_delta" },
      { type: "turn_completed" },
    ]);
  });

  it("reduces live execution progress from the same event fixture for every client", () => {
    const events: SidecarEvent[] = [
      { type: "turn_started", session_id: session.id, turn_id: "turn-1", payload: {} },
      { type: "thinking_delta", session_id: session.id, turn_id: "turn-1", payload: { delta: "Inspecting " } },
      { type: "thinking_delta", session_id: session.id, turn_id: "turn-1", payload: { delta: "files" } },
      { type: "tool_started", session_id: session.id, turn_id: "turn-1", payload: { tool_call_id: "call-1", tool_name: "bash", tool_input: { command: "pwd" } } },
      { type: "tool_finished", session_id: session.id, turn_id: "turn-1", payload: { tool_call_id: "call-1", tool_name: "bash", output: "ok", log_id: "log-1" } },
      { type: "todo_updated", session_id: session.id, payload: { items: [{ content: "Ship", status: "in_progress" }] } },
      { type: "context_usage_updated", session_id: session.id, payload: { context_window_usage: { used_tokens: 12, max_tokens: 100 } } },
      { type: "subagent_activity", session_id: session.id, payload: { name: "Scout", status: "working", text: "Scanning" } },
      { type: "interrupt_requested", session_id: session.id, turn_id: "turn-1", payload: {} },
      { type: "interrupt_completed", session_id: session.id, turn_id: "turn-1", payload: {} },
      { type: "loop_user_message_injected", session_id: session.id, turn_id: "turn-1", payload: { injection_id: "inject-1", text: "Continue" } },
      { type: "loop_user_message_injected", session_id: session.id, turn_id: "turn-1", payload: { injection_id: "inject-1", text: "Continue" } },
      { type: "thinking_finished", session_id: session.id, turn_id: "turn-1", payload: { path: ".open_somnia/thinking/turn-1.jsonl" } },
    ];

    const final = events.reduce((state, event) => transitionConversationEvent(state, event).state, createConversationState(session));

    expect(final.thinking).toMatchObject({ text: "Inspecting files", status: "finished" });
    expect(final.tools).toEqual([expect.objectContaining({ id: "call-1", name: "bash", status: "finished", logId: "log-1" })]);
    expect(final.todoItems).toEqual([{ content: "Ship", status: "in_progress" }]);
    expect(final.contextUsage).toEqual({ used_tokens: 12, max_tokens: 100 });
    expect(final.subagents).toEqual([expect.objectContaining({ name: "Scout", status: "working", text: "Scanning" })]);
    expect(final.interruptStatus).toBe("completed");
    expect(final.injectedUserMessages).toEqual([{ id: "inject-1", text: "Continue" }]);
  });
});
