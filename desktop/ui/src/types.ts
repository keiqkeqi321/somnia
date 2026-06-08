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
  preview?: string;
  has_visible_exchange?: boolean;
  is_summary?: boolean;
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
  vision_provider?: string | null;
  vision_model?: string | null;
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
  max_tokens?: number | null;
  reasoning_level?: string | null;
  supports_reasoning?: boolean | null;
  supports_adaptive_reasoning?: boolean | null;
  is_default: boolean;
  is_active: boolean;
  is_vision: boolean;
}

export interface TurnStartResponse {
  turn_id: string;
  session_id: string;
}

export interface LoopInjectionResponse {
  turn_id: string;
  injection_id: string;
  queued: boolean;
}

export interface WorkspacePathSuggestion {
  path: string;
  basename: string;
  kind: "dir" | "file";
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

export interface ThinkingLogDetail {
  path: string;
  text: string;
}

export interface TeamMemberActivity {
  name: string;
  role?: string;
  status?: string;
  activity?: string;
  current_tool_name?: string | null;
  current_task_id?: number | string | null;
  recent_interactions?: string[];
  summary?: string;
  [key: string]: unknown;
}

export interface TaskGraphItem {
  id: number;
  subject?: string;
  description?: string;
  status?: "pending" | "in_progress" | "completed" | string;
  owner?: string | null;
  preferred_owner?: string | null;
  session_id?: string | null;
  blockedBy?: number[];
  blocks?: number[];
  created_at?: number;
  updated_at?: number;
  [key: string]: unknown;
}

export type SettingsConfigSectionKey = "provider" | "mcp" | "hooks" | "system_prompt";
export type SettingsConfigScopeKey = "user" | "project";

export interface SettingsSkillEntry {
  name: string;
  description: string;
  path: string;
  scope: string;
}

export interface SettingsConfigScope {
  scope: SettingsConfigScopeKey;
  label: string;
  config_path: string;
  config_exists: boolean;
  skills_path: string;
  skills_exists: boolean;
  sections: Record<SettingsConfigSectionKey, string>;
  skills: SettingsSkillEntry[];
}

export interface SettingsConfigPayload {
  scopes: SettingsConfigScope[];
}

export interface SaveSettingsConfigSectionResult {
  scope: string;
  section: string;
  config_path: string;
  saved: boolean;
  restart_required: boolean;
  runtime_reloaded?: boolean;
}

export interface McpToolSummary {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export interface McpServerSummary {
  name: string;
  transport: string;
  target: string;
  enabled: boolean;
  status: string;
  error?: string;
  tool_count: number;
  tools: McpToolSummary[];
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
  parts?: ConversationRowPart[];
  toolCalls?: ConversationToolCall[];
  images?: ConversationImageReferenceBlock[];
}

export type ConversationRowPart =
  | {
      id: string;
      type: "text";
      text: string;
    }
  | {
      id: string;
      type: "thinking_log";
      thinkingLog: ConversationThinkingLog;
    }
  | {
      id: string;
      type: "tool_call";
      toolCall: ConversationToolCall;
    };

export type ConversationRuntimeItem =
  | {
      id: string;
      type: "user_text";
      text: string;
    }
  | {
      id: string;
      type: "assistant_text";
      text: string;
      isStreaming?: boolean;
    }
  | {
      id: string;
      type: "thinking_log";
      thinkingLog: ConversationThinkingLog;
      isStreaming?: boolean;
    }
  | {
      id: string;
      type: "tool_call";
      toolCall: ConversationToolCall;
    };

export interface ConversationThinkingLog {
  turnId?: string | null;
  path?: string | null;
  text?: string;
  characters?: number;
  blockCount?: number;
  durationMs?: number | null;
  status?: "running" | "finished";
}

export interface ConversationToolCall {
  id: string;
  name: string;
  input: string;
  output: string;
  rawInput?: unknown;
  rawOutput?: unknown;
  contentBlocks?: ConversationContentBlock[];
  logId?: string | null;
  status?: "running" | "finished";
}

export interface ConversationImageReferenceBlock {
  type: "image_reference";
  path?: string;
  absolute_path?: string;
  media_type?: string;
  image_url?: string;
  origin?: string;
}

export interface ConversationTextContentBlock {
  type: "text";
  text: string;
}

export type ConversationContentBlock = ConversationImageReferenceBlock | ConversationTextContentBlock;

export interface ConversationPendingTurn {
  id: string;
  sessionId: string | null;
  userText: string;
  placeholderText: string;
}
