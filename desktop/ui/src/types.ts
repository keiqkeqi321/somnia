export interface SessionMessage {
  role: string;
  content: unknown;
  name?: string;
  input?: Record<string, unknown>;
  output?: unknown;
  [key: string]: unknown;
}

export interface TodoItem {
  content?: string;
  status?: string;
  activeForm?: string;
  cancelledReason?: string;
  [key: string]: unknown;
}

export interface AgentSession {
  id: string;
  created_at?: number | null;
  updated_at?: number | null;
  messages: SessionMessage[];
  token_usage: Record<string, number>;
  todo_items: TodoItem[];
  rounds_without_todo: number;
  read_file_overlap_state?: Record<string, unknown>;
  latest_turn_id?: string | null;
  last_turn_file_changes?: Array<Record<string, unknown>>;
  undo_stack?: Array<Record<string, unknown>>;
  context_window_usage?: ContextWindowUsage | null;
}

export interface ContextWindowUsage {
  used_tokens: number;
  max_tokens?: number | null;
  usage_percent?: number | null;
  counter_name?: string;
}

export interface SidecarStatus {
  status: string;
  version: string;
  workspace_root: string;
  base_url: string;
  ws_url: string;
  provider: string;
  model: string;
  reasoning_level?: string | null;
  execution_mode?: string | null;
  execution_mode_title?: string | null;
  pending_interaction_count?: number;
  open_session_count?: number;
}

export interface ManagedSidecarConnection {
  baseUrl: string;
  wsUrl: string;
  workspaceRoot: string;
}

export interface ProviderDescriptor {
  name: string;
  provider_type: string;
  default_model: string;
  models: string[];
  active_model?: string | null;
  reasoning_level?: string | null;
  is_active: boolean;
}

export interface ModelDescriptor {
  provider_name: string;
  name: string;
  context_window_tokens?: number | null;
  supports_reasoning?: boolean | null;
  supports_adaptive_reasoning?: boolean | null;
  is_default: boolean;
  is_active: boolean;
}

export interface TurnStartResponse {
  turn_id: string;
  session_id: string;
}

export interface InteractionRequestState {
  id: string;
  kind: string;
  session_id?: string | null;
  turn_id?: string | null;
  payload: Record<string, unknown>;
  response?: Record<string, unknown> | null;
}

export interface ToolLogIndexEntry {
  id: string;
  timestamp: number;
  actor: string;
  tool_name: string;
  category: string;
  path: string;
}

export interface ToolLogDetail extends ToolLogIndexEntry {
  tool_input: Record<string, unknown>;
  output?: unknown;
  rendered: string;
}

export interface SidecarEvent {
  type: string;
  session_id?: string | null;
  turn_id?: string | null;
  payload: Record<string, unknown>;
  timestamp?: number;
}

export interface ConversationRow {
  id: string;
  role: "user" | "assistant";
  text: string;
  isStreaming?: boolean;
  isLoading?: boolean;
  isPending?: boolean;
  toolCalls?: ConversationToolCall[];
}

export interface ConversationToolCall {
  id: string;
  name: string;
  input: string;
  output: string;
  logId?: string | null;
}

export interface ConversationPendingTurn {
  id: string;
  sessionId: string | null;
  userText: string;
  placeholderText: string;
}
