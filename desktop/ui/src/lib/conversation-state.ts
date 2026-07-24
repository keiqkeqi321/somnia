import type { AgentSession, SidecarEvent } from "../types";

export interface ConversationState {
  sessionId: string;
  session: AgentSession | null;
  activeTurnId: string | null;
  assistantText: string;
  thinking: { text: string; status: "running" | "finished"; path: string | null } | null;
  tools: ConversationProgressTool[];
  todoItems: unknown[];
  contextUsage: Record<string, unknown> | null;
  subagents: Array<Record<string, unknown>>;
  injectedUserMessages: Array<{ id: string; text: string }>;
  interruptStatus: "requested" | "completed" | null;
}

export interface ConversationProgressTool {
  id: string;
  name: string;
  input: unknown;
  output: unknown;
  status: "running" | "finished";
  logId: string | null;
  contentBlocks: unknown[];
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
  const loaded = typeof session === "string" ? null : session;
  return {
    sessionId: typeof session === "string" ? session : session.id,
    session: typeof session === "string" ? null : session,
    activeTurnId: null,
    assistantText: "",
    thinking: null,
    tools: [],
    todoItems: Array.isArray(loaded?.todo_items) ? loaded.todo_items : [],
    contextUsage: isRecord(loaded?.context_window_usage) ? loaded.context_window_usage : null,
    subagents: [],
    injectedUserMessages: [],
    interruptStatus: null,
  };
}

export function transitionConversationEvent(state: ConversationState, event: SidecarEvent): ConversationTransition {
  if (event.session_id && event.session_id !== state.sessionId) {
    return unchanged(state);
  }
  if (event.type === "turn_started") {
    const turnId = event.turn_id ?? null;
    return {
      state: {
        ...state,
        activeTurnId: turnId,
        assistantText: "",
        thinking: null,
        tools: [],
        subagents: [],
        injectedUserMessages: [],
        interruptStatus: null,
      },
      effect: { type: "turn_started" },
    };
  }
  if (event.type === "interrupt_requested") {
    return changed(state, { interruptStatus: "requested" });
  }
  if (event.type === "interrupt_completed") {
    return changed(state, { interruptStatus: "completed" });
  }
  if (event.type === "thinking_delta") {
    const delta = text(event.payload.delta ?? event.payload.text);
    return delta ? changed(state, {
      thinking: {
        text: `${state.thinking?.text ?? ""}${delta}`,
        status: "running",
        path: state.thinking?.path ?? null,
      },
    }) : unchanged(state);
  }
  if (event.type === "thinking_finished") {
    return changed(state, {
      thinking: {
        text: text(event.payload.text) || state.thinking?.text || "",
        status: "finished",
        path: text(event.payload.path) || state.thinking?.path || null,
      },
    });
  }
  if (event.type === "tool_started") {
    const id = text(event.payload.tool_call_id) || `tool-${state.tools.length + 1}`;
    const tool: ConversationProgressTool = {
      id,
      name: text(event.payload.tool_name) || "tool",
      input: event.payload.tool_input ?? {},
      output: null,
      status: "running",
      logId: null,
      contentBlocks: [],
    };
    return changed(state, { tools: [...state.tools.filter((item) => item.id !== id), tool] });
  }
  if (event.type === "tool_finished") {
    const explicitId = text(event.payload.tool_call_id);
    const name = text(event.payload.tool_name) || "tool";
    let index = explicitId ? state.tools.findIndex((item) => item.id === explicitId) : -1;
    if (index < 0) {
      for (let candidate = state.tools.length - 1; candidate >= 0; candidate -= 1) {
        if (state.tools[candidate].name === name && state.tools[candidate].status === "running") {
          index = candidate;
          break;
        }
      }
    }
    const previous = index >= 0 ? state.tools[index] : null;
    const finished: ConversationProgressTool = {
      id: previous?.id ?? (explicitId || `tool-${state.tools.length + 1}`),
      name,
      input: event.payload.tool_input ?? previous?.input ?? {},
      output: event.payload.output ?? null,
      status: "finished",
      logId: text(event.payload.log_id) || null,
      contentBlocks: Array.isArray(event.payload.content_blocks) ? event.payload.content_blocks : [],
    };
    const tools = [...state.tools];
    if (index >= 0) tools[index] = finished;
    else tools.push(finished);
    return changed(state, { tools });
  }
  if (event.type === "todo_updated" && Array.isArray(event.payload.items)) {
    return changed(state, { todoItems: event.payload.items });
  }
  if (event.type === "context_usage_updated" && isRecord(event.payload.context_window_usage)) {
    return changed(state, { contextUsage: event.payload.context_window_usage });
  }
  if (event.type === "subagent_activity") {
    const name = text(event.payload.name ?? event.payload.actor) || "worker";
    return changed(state, {
      subagents: [...state.subagents.filter((item) => text(item.name ?? item.actor) !== name), { ...event.payload, name }],
    });
  }
  if (event.type === "loop_user_message_injected") {
    const id = text(event.payload.injection_id);
    const messageText = text(event.payload.text) || text(event.payload.user_input);
    if (!id || !messageText || state.injectedUserMessages.some((message) => message.id === id)) {
      return unchanged(state);
    }
    return changed(state, {
      injectedUserMessages: [...state.injectedUserMessages, { id, text: messageText }],
    });
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
      state: {
        ...state,
        session,
        activeTurnId: null,
        assistantText: "",
        todoItems: Array.isArray(session?.todo_items) ? session.todo_items : state.todoItems,
        contextUsage: isRecord(session?.context_window_usage) ? session.context_window_usage : state.contextUsage,
      },
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

function changed(state: ConversationState, patch: Partial<ConversationState>): ConversationTransition {
  return { state: { ...state, ...patch }, effect: { type: "none" } };
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
