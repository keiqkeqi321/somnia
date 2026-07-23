import type { AgentSession, SidecarEvent } from "../types";

export interface ConversationState {
  sessionId: string;
  session: AgentSession | null;
  activeTurnId: string | null;
  assistantText: string;
}

export type ConversationEffect =
  | { type: "none" }
  | { type: "turn_started" }
  | { type: "assistant_delta" }
  | { type: "turn_completed" };

export interface ConversationTransition {
  state: ConversationState;
  effect: ConversationEffect;
}

export function createConversationState(session: AgentSession | string): ConversationState {
  return {
    sessionId: typeof session === "string" ? session : session.id,
    session: typeof session === "string" ? null : session,
    activeTurnId: null,
    assistantText: "",
  };
}

export function transitionConversationEvent(state: ConversationState, event: SidecarEvent): ConversationTransition {
  if (event.session_id && event.session_id !== state.sessionId) {
    return unchanged(state);
  }
  if (event.type === "turn_started") {
    const turnId = event.turn_id ?? null;
    return {
      state: { ...state, activeTurnId: turnId, assistantText: "" },
      effect: { type: "turn_started" },
    };
  }
  if (event.type === "assistant_delta") {
    const delta = typeof event.payload.delta === "string" ? event.payload.delta : "";
    return delta
      ? {
          state: { ...state, assistantText: `${state.assistantText}${delta}` },
          effect: { type: "assistant_delta" },
        }
      : unchanged(state);
  }
  if (event.type === "turn_result") {
    const session = readSessionPayload(event.payload.session) ?? state.session;
    return {
      state: { ...state, session, activeTurnId: null, assistantText: "" },
      effect: { type: "turn_completed" },
    };
  }
  return unchanged(state);
}

export function readSessionPayload(value: unknown): AgentSession | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const session = value as Partial<AgentSession>;
  if (typeof session.id !== "string" || !Array.isArray(session.messages)) {
    return null;
  }
  return value as AgentSession;
}

function unchanged(state: ConversationState): ConversationTransition {
  return { state, effect: { type: "none" } };
}
