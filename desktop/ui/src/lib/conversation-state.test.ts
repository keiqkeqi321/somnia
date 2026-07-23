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

    expect(initial).toEqual({ sessionId: session.id, session, activeTurnId: null, assistantText: "" });
    expect(final).toEqual({
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
});
