"""Agent 运行时模块.

提供 OpenAgent 的核心运行时功能，包括：
- LLM 提供者管理
- 工具注册和执行
- 会话管理
- 子代理运行
- 后台任务管理
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
import os
import re
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

from open_somnia.collaboration.bus import MessageBus
from open_somnia.collaboration.protocols import RequestTracker
from open_somnia.config.models import AppSettings, HookSettings, ModelTraits, ProviderProfileSettings, ProviderSettings
from open_somnia.config.settings import (
    _load_mcp_servers,
    _materialize_provider,
    _merge_config,
    _normalize_model_id,
    _read_toml,
    global_config_path,
    load_settings,
    persist_hook_enabled,
    persist_mcp_tool_enabled,
    persist_provider_reasoning_level,
    persist_provider_selection,
    persist_vision_model,
    workspace_config_path,
)
from open_somnia.config.settings import BUILTIN_NOTIFY_MANAGER
from open_somnia.hooks.manager import HookManager
from open_somnia.mcp.registry import MCPRegistry
from open_somnia.providers.base import LLMProvider, ProviderError
from open_somnia.runtime.compact import (
    AUTO_COMPACT_TRIGGER_RATIO,
    CompactManager,
    ContextWindowUsage,
    build_payload_messages,
    context_pressure_level,
    estimate_payload_tokens,
    should_auto_compact,
)
from open_somnia.runtime.execution_mode import (
    ASK_USER_QUESTION_TOOL_NAME,
    AUTHORIZATION_TOOL_NAME,
    DEFAULT_EXECUTION_MODE,
    MODE_SWITCH_TOOL_NAME,
    NEW_SESSION_TOOL_NAME,
    NON_YOLO_EXECUTION_MODES,
)
from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import (
    consume_ephemeral_image_blocks,
    decode_embedded_user_message,
    make_tool_result_item,
    make_tool_result_message,
    make_user_text_message,
    render_text_content,
)
from open_somnia.runtime.permissions import PermissionManager
from open_somnia.runtime.parallel_dispatch import (
    dispatch_parallel_segment,
    is_explore_subagent_safe,
    is_parallel_safe,
    parallel_dispatch_enabled,
    run_parallel_explore_subagents,
    segment_tool_calls,
)
from open_somnia.runtime.project_instructions import ProjectInstructionsLoader
from open_somnia.runtime.round_runner import ToolCallRecord, execute_tool_call, finalize_tool_call
from open_somnia.runtime.session import AgentSession, SessionManager
from open_somnia.runtime.subagent_runner import SubagentRunner
from open_somnia.runtime.system_prompt import SystemPromptBuilder
from open_somnia.runtime.prompt_sections import cache_optimized_system_prompt
from open_somnia.runtime.teammate import TeammateRuntimeManager
from open_somnia.runtime.thinking import ThinkingLogWriter, extract_thinking_blocks, is_thinking_block, strip_thinking_log_blocks_from_message
from open_somnia.runtime.tool_events import ToolEventRenderer
from open_somnia.skills.loader import SkillLoader
from open_somnia.storage.inbox import InboxStore
from open_somnia.storage.jobs import JobStore
from open_somnia.storage.sessions import SessionStore
from open_somnia.storage.common import atomic_write_text
from open_somnia.storage.subagent_checkpoints import SubagentCheckpointStore
from open_somnia.storage.subagent_logs import SubagentLogStore
from open_somnia.storage.tasks import TaskStore
from open_somnia.storage.team import TeamStore
from open_somnia.storage.tool_logs import ToolLogStore
from open_somnia.storage.transcripts import TranscriptStore
from open_somnia.tools.background import BackgroundManager, register_background_tools
from open_somnia.tools.filesystem import _read_text_with_fallback, safe_path
from open_somnia.tools.filesystem import register_filesystem_tools
from open_somnia.tools.mcp import register_mcp_tools
from open_somnia.tools.registry import ToolDefinition, ToolRegistry
from open_somnia.tools.shell import register_shell_tool
from open_somnia.tools.subagent import register_subagent_tool
from open_somnia.tools.tasks import register_task_tools
from open_somnia.tools.team import register_team_tools
from open_somnia.tools.tool_errors import (
    make_tool_error,
    render_transient_repair_hint_message,
    serialize_tool_output,
)
from open_somnia.tools.todo import TodoManager, register_todo_tool
from open_somnia.tools.web_fetch import register_web_fetch_tool
from open_somnia.reasoning import normalize_reasoning_level


OpenAIProvider = None
AnthropicProvider = None


@dataclass(slots=True)
class _PlannedLeadCall:
    """One tool call's deterministic plan from the lead pre-scan."""

    tool_call: Any
    tool_name: str
    is_exploration: bool
    # "execute" | "flood_error" | "malformed_error" | "unknown_error" | "budget_error"
    decision: str
    guard_output: Any  # non-None only for the *_error decisions
    parallel_safe: bool  # decision == "execute" and is_parallel_safe(tool_name)
    is_turn_boundary: bool  # tool_name in TURN_BOUNDARY_TOOL_NAMES
    end_turn_after: bool  # flood/budget guard tripped at or before this call


class AgentLoopResult(str):
    __slots__ = ("status", "open_todo_count")

    def __new__(
        cls,
        text: str = "",
        *,
        status: str = "completed",
        open_todo_count: int = 0,
    ):
        obj = str.__new__(cls, text)
        obj.status = str(status or "completed")
        obj.open_todo_count = max(0, int(open_todo_count or 0))
        return obj


class OpenAgentRuntime:
    DEBUG_PROVIDER_PAYLOAD_ENV = "SOMNIA_DEBUG_PROVIDER_PAYLOADS"
    EXPLORATION_SOFT_LIMIT = 10
    EXPLORATION_HARD_STREAK_LIMIT = 14
    EXPLORATION_HARD_TOTAL_LIMIT = 0
    EXPLORATION_TOOL_NAMES = frozenset({"tree", "glob", "grep", "read_file", "find_symbol"})
    EXPLORATION_GITNEXUS_TOOL_NAMES = frozenset(
        {
            "api_impact",
            "context",
            "cypher",
            "group_list",
            "impact",
            "query",
            "route_map",
            "shape_check",
            "tool_map",
        }
    )
    EXPLORATION_SHELL_PREFIXES = (
        "cat ",
        "dir",
        "find ",
        "get-childitem",
        "get-content",
        "git diff",
        "git log",
        "git show",
        "git status",
        "grep ",
        "head ",
        "ls",
        "pwd",
        "rg ",
        "select-string",
        "tail ",
        "tree",
        "type ",
    )
    TODO_REMINDER_TEXT = (
        "Reminder: If any todo changed, call TodoWrite now. "
        "Do not just say you will. If nothing changed, ignore this and continue."
    )
    EXPLORATION_SUMMARY_REMINDER_TEXT = (
        "Reminder: You have been exploring for {streak} consecutive read/search step(s) "
        "({total} total this turn). Stop exploring in the main context now — delegate the remaining "
        "exploration to a `subagent` (it runs in its own clean context) or provide a concise interim "
        "conclusion first: state the likely finding, evidence already gathered, confidence, and the "
        "smallest remaining verification if any."
    )
    TODO_RECONCILE_REMINDER_TEXT = (
        "Reminder: Before ending, reconcile TodoWrite with the work just completed. "
        "If any todo changed, call TodoWrite now. If the current todo list is already accurate, end the turn without extra prose."
    )
    TODO_STALE_STATUS_REMINDER_TEXT = "Reminder: Pay attention to the status of todos"
    EMPTY_ASSISTANT_RESPONSE_REPAIR_TEXT = (
        "Reminder: Your previous response ended without any visible assistant text or tool calls. "
        "Continue the task now and either call the next needed tool or provide a visible final answer. "
        "Do not re-plan at length — act now and keep any internal reasoning short."
    )
    EMPTY_RESPONSE_REASONING_BUMP_NOTICE = (
        "Runtime notice: the model spent the entire max_tokens budget on reasoning and produced no "
        "visible output. Temporarily raising the completion budget so reasoning does not crowd out the "
        "answer. If this repeats, the turn will be stopped."
    )
    EMPTY_RESPONSE_STOPPED_TEXT = (
        "Stopped: the model produced only internal reasoning with no visible text or tool calls for "
        "several consecutive turns (likely the completion budget is too small for this reasoning model). "
        "Raise the provider max_tokens, lower the reasoning level, or switch to a non-reasoning model."
    )
    RUNTIME_NOTICE_PREFIX = "Runtime notice:"
    # Context-pressure early warnings. Unlike transient notices these are real
    # persisted user messages (appended at the top of the next loop round), so
    # the model keeps seeing them and the compaction summary inherits them.
    CONTEXT_PRESSURE_SOFT_MESSAGE_TEMPLATE = (
        "<context-pressure level=\"soft\">\n"
        "Context window usage has reached about {percent}% ({used} of {max} tokens via {counter}). "
        "Lossy auto-compaction triggers at {trigger}.\n"
        "Plan the end of this stage now, while you still hold the full working context:\n"
        "- Finish the current atomic step; avoid opening new large explorations in this window.\n"
        "- Externalize state: update the todo list / task board so the next window can rebuild from it.\n"
        "- When the stage is done, call request_new_session with a handoff covering the goal, "
        "confirmed decisions, files changed, open work, and next steps.\n"
        "</context-pressure>"
    )
    CONTEXT_PRESSURE_URGENT_MESSAGE_TEMPLATE = (
        "<context-pressure level=\"urgent\">\n"
        "Context window usage has reached about {percent}% ({used} of {max} tokens via {counter}); "
        "lossy auto-compaction fires at {trigger} — imminent.\n"
        "Wrap up now: finish or safely park the in-flight edit, do not start anything new, then call "
        "request_new_session with a full handoff (goal, confirmed decisions, files changed, open work, "
        "next steps) so the next window continues cleanly instead of losing detail to compaction.\n"
        "</context-pressure>"
    )
    TOOL_IMPORTANCE_VALUES = ("glance", "investigate", "foundation")
    TOOL_VALUE_PREVIEW_CHARS = 90
    TOOL_RESULT_PREVIEW_CHARS = 60
    SILENT_TOOL_NAMES = {"TodoWrite"}
    MAX_UNDO_TURNS = 10
    TURN_BOUNDARY_TOOL_NAMES = {AUTHORIZATION_TOOL_NAME, MODE_SWITCH_TOOL_NAME}
    DEFAULT_MAX_TOOL_CALLS_PER_TURN = 64
    WORKSPACE_PERMISSIONS_FILE = "permissions.json"
    BUILTIN_PERMISSIONS_FILE = "builtin/permissions.json"
    PROVIDER_POLL_INTERVAL_SECONDS = 0.1
    PROVIDER_RETRY_DELAY_SECONDS = 2.0
    # Empty-response (thinking-only) circuit breaker. When a reasoning model burns
    # the whole max_tokens budget on thinking and emits no visible text or tool
    # calls, retrying with the same budget loops indefinitely. Allow a few nudges
    # first, then stop the turn with a clear diagnostic instead of spinning up to
    # max_agent_rounds. See _detect_reasoning_budget_exhaustion for the paired
    # usage-based auto-bump that tries to recover before giving up.
    EMPTY_RESPONSE_MAX_STREAK = 3
    EMPTY_RESPONSE_REASONING_BUMP_FACTOR = 2.0
    EMPTY_RESPONSE_REASONING_BUMP_MAX_TOKENS = 65_536
    EMPTY_RESPONSE_REASONING_BUDGET_RATIO = 0.90
    _ansi_output_enabled: bool | None = None
    DEFAULT_SYSTEM_PROMPT_TEMPLATE = (
        "You are {name}, a top-rated AI assistant.\n"
        "You are exceptionally strong at coding tasks, software design, debugging, implementation, and complex reasoning.\n"
        "You solve problems with clear, defensible thinking, strong technical judgment, and careful tool use.\n"
        "Be precise, pragmatic, and direct. Prefer concrete actions over vague advice.\n"
        "When needed, inspect the workspace and use tools to verify assumptions before acting."
    )

    """OpenAgent 运行时类.

    管理代理的完整运行时环境，包括工具、会话、任务等。

    Attributes:
        settings: 应用配置。
        provider: LLM 提供者。
        transcript_store: 转录存储。
        session_manager: 会话管理器。
        task_store: 任务存储。
        job_store: 后台任务存储。
        inbox_store: 收件箱存储。
        bus: 消息总线。
        team_store: 团队存储。
        request_tracker: 请求跟踪器。
        skill_loader: 技能加载器。
        todo_manager: 待办事项管理器。
        background_manager: 后台任务管理器。
        compact_manager: 压缩管理器。
        mcp_registry: MCP 注册表。
        team_manager: 团队管理器。
        registry: 主工具注册表。
        worker_registry: 工作器工具注册表。
    """

    def __init__(self, settings: AppSettings) -> None:
        """初始化 OpenAgent 运行时.

        Args:
            settings: 应用配置对象。
        """
        self.settings = settings
        self.execution_mode = DEFAULT_EXECUTION_MODE
        self.authorization_request_handler = None
        self.mode_switch_request_handler = None
        self.ask_user_question_handler = None
        # Sessions the agent asked to swap out of (via request_new_session),
        # keyed by session id -> handoff text. Consumed at the turn boundary
        # by whoever drives the session (REPL runner / RuntimeHost).
        self._pending_new_sessions: dict[str, str] = {}
        self.permission_manager = PermissionManager(self)
        self.subagent_runner = SubagentRunner(self)
        self.system_prompt_builder = SystemPromptBuilder(self)
        self._builtin_authorized_tools = self._load_builtin_authorizations()
        self._workspace_authorized_tools = self._load_workspace_authorizations()
        self._once_authorized_tools: dict[str, int] = {}
        self._worker_authorized_tools: set[str] = set()
        self._worker_once_authorized_tools: dict[str, int] = {}
        self.provider = self._make_provider()
        self.transcript_store = TranscriptStore(settings.storage.transcripts_dir)
        self.session_manager = SessionManager(SessionStore(settings.storage.sessions_dir), self.transcript_store)
        self.task_store = TaskStore(settings.storage.tasks_dir)
        self.job_store = JobStore(settings.storage.jobs_dir)
        self.tool_log_store = ToolLogStore(settings.storage.logs_dir)
        self.subagent_log_store = SubagentLogStore(settings.storage.logs_dir)
        self.subagent_checkpoint_store = SubagentCheckpointStore(settings.storage.logs_dir)
        self.inbox_store = InboxStore(settings.storage.inbox_dir)
        self.bus = MessageBus(self.inbox_store)
        self.team_store = TeamStore(settings.storage.team_dir)
        self.request_tracker = RequestTracker(settings.storage.requests_dir)
        self.skill_loader = SkillLoader.for_workspace(settings.workspace_root)
        self.todo_manager = TodoManager()
        self.hook_manager = HookManager(settings)
        self.background_manager = BackgroundManager(
            self.job_store,
            settings.workspace_root,
            settings.runtime.command_timeout_seconds,
            settings.runtime.max_tool_output_chars,
        )
        self.compact_manager = CompactManager(self.provider, self.transcript_store, settings.provider.max_tokens)
        self._context_usage_cache: dict[str, tuple[tuple[Any, ...], ContextWindowUsage]] = {}
        self._payload_message_cache: dict[str, tuple[tuple[Any, ...], list[dict[str, Any]]]] = {}
        self._recent_context_usage: dict[str, ContextWindowUsage] = {}
        self._context_governance_events: dict[str, dict[str, Any]] = {}
        self._current_working_file: dict[str, Any] | None = None
        # One-shot max_tokens override for reasoning-budget recovery (see
        # _maybe_raise_reasoning_budget / _consume_transient_max_tokens_override).
        self._transient_max_tokens_override: int | None = None
        self.mcp_registry = MCPRegistry(settings.mcp_servers)
        self.team_manager = TeammateRuntimeManager(
            runtime=self,
            team_store=self.team_store,
            bus=self.bus,
            task_store=self.task_store,
            request_tracker=self.request_tracker,
        )
        self.registry = ToolRegistry()
        self.worker_registry = ToolRegistry()
        self.tool_event_renderer = ToolEventRenderer(self)
        self._register_core_tools(self.registry)
        self.register_worker_tools(self.worker_registry)

    def _tool_event_renderer(self) -> ToolEventRenderer:
        renderer = getattr(self, "tool_event_renderer", None)
        if renderer is None:
            renderer = ToolEventRenderer(self)
            self.tool_event_renderer = renderer
        return renderer

    def _hook_manager(self) -> HookManager:
        manager = getattr(self, "hook_manager", None)
        if manager is None:
            manager = HookManager(self.settings)
            self.hook_manager = manager
        return manager

    def _permission_manager(self) -> PermissionManager:
        manager = getattr(self, "permission_manager", None)
        if manager is None:
            manager = PermissionManager(self)
            self.permission_manager = manager
        return manager

    def _system_prompt_builder(self) -> SystemPromptBuilder:
        builder = getattr(self, "system_prompt_builder", None)
        if builder is None:
            builder = SystemPromptBuilder(self)
            self.system_prompt_builder = builder
        return builder

    def _subagent_runner(self) -> SubagentRunner:
        runner = getattr(self, "subagent_runner", None)
        if runner is None:
            runner = SubagentRunner(self)
            self.subagent_runner = runner
        return runner

    def print_tool_event(self, actor: str, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
        return self._tool_event_renderer().print_tool_event(actor, tool_name, tool_input, output)

    def print_tool_started(
        self,
        actor: str,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> None:
        self._tool_event_renderer().print_tool_started(actor, tool_name, tool_input)

    def print_tool_finished(
        self,
        actor: str,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Notify the host that a tool call has finished.

        The mirror of :meth:`print_tool_started`. Most tools finish through
        ``print_tool_event`` (the rendered result) plus the ``registry.execute``
        wrapper; the parallel Explore-subagent path bypasses ``registry.execute``
        entirely, so the lead loop calls this explicitly to let the host clear
        the active-tool / active-subagent slot keyed by ``tool_call_id``.
        """
        # No terminal rendering here: the finished state is conveyed by the
        # subsequent ``print_tool_event`` call. This method exists for host
        # bookkeeping (REPL active-subagent map, desktop TOOL_FINISHED event).
        return None

    def render_tool_event_lines(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        output: Any,
        *,
        log_id: str | None = None,
    ) -> list[str]:
        return self._tool_event_renderer().render_tool_event_lines(tool_name, tool_input, output, log_id=log_id)

    def _capture_turn_file_changes(self, session: AgentSession) -> None:
        pending = list(getattr(session, "pending_file_changes", []) or [])
        session.pending_file_changes = []
        if not pending:
            session.last_turn_file_changes = []
            return
        session.last_turn_file_changes = self._tool_event_renderer().summarize_file_changes(pending)
        session.undo_stack.append(
            {
                "turn_id": session.latest_turn_id,
                "files": pending,
            }
        )
        if len(session.undo_stack) > self.MAX_UNDO_TURNS:
            session.undo_stack = session.undo_stack[-self.MAX_UNDO_TURNS :]

    def note_active_file(
        self,
        *,
        path: str,
        content: str,
        source: str,
        snippet: str | None = None,
    ) -> None:
        normalized_path = str(path).strip().replace("\\", "/")
        if not normalized_path:
            return
        preview = str(snippet if snippet is not None else content).strip()
        if len(preview) > 1600:
            preview = preview[:1597] + "..."
        self._current_working_file = {
            "path": normalized_path,
            "content": str(content),
            "source": str(source).strip() or "tool",
            "snippet": preview,
            "line_count": len(str(content).splitlines()),
            "updated_at": time.monotonic(),
        }

    def current_working_file_context(self) -> str:
        entry = getattr(self, "_current_working_file", None)
        if not isinstance(entry, dict):
            return ""
        path = str(entry.get("path", "")).strip()
        if not path:
            return ""
        source = str(entry.get("source", "tool")).strip() or "tool"
        line_count = int(entry.get("line_count", 0) or 0)
        snippet = str(entry.get("snippet", "")).strip()
        if not snippet:
            snippet = self._context_compact_text(str(entry.get("content", "")), limit=900)
        if not snippet:
            return ""
        return (
            "Active working file cache:\n"
            f"- Path: {path}\n"
            f"- Source: {source}\n"
            f"- Lines: {line_count}\n"
            "- Prefer this cached snapshot over rereading the same file when you are still editing the same area.\n"
            "Cached snapshot:\n"
            f"{snippet}"
        )

    def current_working_file_path(self) -> str:
        entry = getattr(self, "_current_working_file", None)
        if not isinstance(entry, dict):
            return ""
        return str(entry.get("path", "")).strip()

    def print_last_turn_file_summary(self, session: AgentSession) -> bool:
        return self._tool_event_renderer().print_last_turn_file_summary(session)

    def undo_last_turn(self, session: AgentSession) -> str:
        undo_stack = list(getattr(session, "undo_stack", []) or [])
        if not undo_stack:
            return "Nothing to undo."
        entry = undo_stack.pop()
        workspace_root = self.settings.workspace_root.resolve()
        for item in reversed(entry.get("files", [])):
            relative_path = str(item.get("path", "")).strip()
            if not relative_path:
                continue
            path = (workspace_root / relative_path).resolve()
            if not path.is_relative_to(workspace_root):
                raise ValueError(f"Undo path escapes workspace: {relative_path}")
            existed_before = bool(item.get("existed_before"))
            previous_content = str(item.get("previous_content", ""))
            if existed_before:
                atomic_write_text(path, previous_content)
            elif path.exists():
                path.unlink()
        session.undo_stack = undo_stack
        session.last_turn_file_changes = []
        session.pending_file_changes = []
        self.session_manager.save(session)
        file_count = len(entry.get("files", []))
        return f"Undid {file_count} file change(s) from the most recent change set."

    def _supports_ansi_output(self) -> bool:
        return self._tool_event_renderer()._supports_ansi_output()

    def _stringify_tool_value(self, value: Any) -> str:
        return self._tool_event_renderer()._stringify_tool_value(value)

    def _compact_preview(self, text: str, *, limit: int) -> str:
        return self._tool_event_renderer()._compact_preview(text, limit=limit)

    def _preview_tool_text(self, text: str, *, limit: int | None = None) -> tuple[str, bool]:
        return self._tool_event_renderer()._preview_tool_text(text, limit=limit)

    def _format_clickable_file_label(self, label: str, absolute_path: str) -> str:
        return self._tool_event_renderer()._format_clickable_file_label(label, absolute_path)

    def recent_tool_logs(self, limit: int = 10) -> str:
        return self._tool_event_renderer().recent_tool_logs(limit=limit)

    def render_tool_log(self, log_id: str) -> str:
        return self._tool_event_renderer().render_tool_log(log_id)

    def render_team_log(self, name: str) -> str:
        manager = getattr(self, "team_manager", None)
        renderer = getattr(manager, "render_log", None)
        if not callable(renderer):
            return f"Teammate '{name}' not found."
        return renderer(name)

    def _make_provider(self) -> LLMProvider:
        return self._instantiate_provider(self.settings.provider)

    def _instantiate_provider(self, provider_settings: ProviderSettings) -> LLMProvider:
        global OpenAIProvider, AnthropicProvider
        if provider_settings.provider_type == "openai":
            if OpenAIProvider is None:
                from open_somnia.providers.openai_provider import OpenAIProvider as _OpenAIProvider

                OpenAIProvider = _OpenAIProvider

            return OpenAIProvider(provider_settings)
        if AnthropicProvider is None:
            from open_somnia.providers.anthropic_provider import AnthropicProvider as _AnthropicProvider

            AnthropicProvider = _AnthropicProvider

        return AnthropicProvider(provider_settings)

    def _messages_include_image_input(self, messages: list[dict[str, Any]]) -> bool:
        def visit(value: Any) -> bool:
            if isinstance(value, dict):
                block_type = str(value.get("type", "")).strip()
                if block_type in {"input_image", "image_url"}:
                    return True
                return any(visit(item) for item in value.values())
            if isinstance(value, list):
                return any(visit(item) for item in value)
            return False

        return visit(messages)

    def _provider_for_messages(self, messages: list[dict[str, Any]]) -> LLMProvider | None:
        active_provider = getattr(self, "provider", None)
        if active_provider is None:
            return None
        vision_provider = str(getattr(self.settings, "vision_provider", "") or "").strip().lower()
        vision_model = str(getattr(self.settings, "vision_model", "") or "").strip()
        if not vision_provider or not vision_model:
            return active_provider
        if not self._messages_include_image_input(messages):
            return active_provider
        profile = self.settings.provider_profiles.get(vision_provider)
        if profile is None:
            raise ValueError(f"Vision provider '{vision_provider}' is not configured.")
        return self._instantiate_provider(_materialize_provider(profile, vision_model))

    def configured_provider_profiles(self) -> dict[str, ProviderProfileSettings]:
        return dict(self.settings.provider_profiles)

    def configured_hooks(self) -> list[HookSettings]:
        return list(getattr(self.settings, "hooks", []) or [])

    def _workspace_authorizations_path(self) -> Path | None:
        return self._permission_manager().workspace_authorizations_path()

    def _load_workspace_authorizations(self) -> set[str]:
        return self._permission_manager().load_workspace_authorizations()

    def _load_builtin_authorizations(self) -> set[str]:
        return self._permission_manager().load_builtin_authorizations()

    def _persist_workspace_authorizations(self) -> None:
        self._permission_manager().persist_workspace_authorizations()

    def authorize_tool_call(self, tool_name: str, payload: dict[str, Any], *, ctx=None) -> str | None:
        return self._permission_manager().authorize_tool_call(tool_name, payload, ctx=ctx)

    def _authorize_subagent_call(self, payload: dict[str, Any]) -> str | None:
        return self._permission_manager()._authorize_subagent_call(payload)

    def request_authorization(self, tool_name: str, reason: str, argument_summary: str = "") -> str:
        return self._permission_manager().request_authorization(tool_name, reason, argument_summary)

    def request_mode_switch(self, target_mode: str, reason: str = "") -> str:
        return self._permission_manager().request_mode_switch(target_mode, reason)

    def request_new_session(self, handoff: str = "", *, session_id: str) -> str:
        """Queue a session swap for the turn boundary (request_new_session tool).

        Auto-approved: the previous session stays persisted and resumable, so
        this never needs a confirmation round-trip. The swap itself is executed
        by the session driver (REPL runner / RuntimeHost consumer) once the
        current turn has finished.
        """
        normalized_handoff = str(handoff or "").strip()
        normalized_session = str(session_id or "").strip()
        if not normalized_session:
            return "request_new_session failed: session_id is required."
        pending = self._new_session_pendings()
        if normalized_session in pending:
            return json.dumps(
                {
                    "status": "already_pending",
                    "handoff": pending[normalized_session],
                    "message": "A new session is already queued for this turn boundary; stop the turn now.",
                },
                ensure_ascii=False,
            )
        pending[normalized_session] = normalized_handoff
        return json.dumps(
            {
                "status": "approved",
                "handoff": normalized_handoff,
                "message": (
                    "This turn ends now. A fresh session starts immediately"
                    + (
                        " with your handoff text as its first prompt. Make sure the handoff carries everything the new context needs."
                        if normalized_handoff
                        else ". No handoff text was given, so the new session starts blank."
                    )
                ),
            },
            ensure_ascii=False,
        )

    def consume_pending_new_session(self, session_id: str) -> str | None:
        """Pop the handoff text queued by request_new_session, if any."""
        return self._new_session_pendings().pop(str(session_id or "").strip(), None)

    def _new_session_pendings(self) -> dict[str, str]:
        # Lazy so __new__-built test runtimes (no __init__) still work.
        return self.__dict__.setdefault("_pending_new_sessions", {})

    def _new_session_pending_for(self, tool_name: str, session: AgentSession) -> bool:
        """True when a request_new_session call just queued a swap, so the agent
        loop must end the turn right after this tool call."""
        return tool_name == NEW_SESSION_TOOL_NAME and session.id in self._new_session_pendings()

    def ask_user_question(self, question: str, options: list[str], allow_custom: bool = True) -> str:
        normalized_question = str(question).strip()
        if not normalized_question:
            return "ask_user_question failed: question is required."
        normalized_options = [str(option).strip() for option in options if str(option).strip()]
        if len(normalized_options) < 2:
            return "ask_user_question failed: at least two options are required."
        handler = self.ask_user_question_handler
        if not callable(handler):
            return "ask_user_question failed: interactive questions are unavailable in this session."
        try:
            hook_manager = self._hook_manager()
        except Exception:
            hook_manager = None
        if hook_manager is not None:
            hook_manager.on_user_choice_requested(
                session=None,
                trace_id=None,
                actor="lead",
                execution_mode=getattr(self, "execution_mode", DEFAULT_EXECUTION_MODE),
                choice_type="ask_user_question",
                choice_payload={
                    "question": normalized_question,
                    "options": list(normalized_options),
                    "allow_custom": bool(allow_custom),
                },
                options=list(normalized_options) + (["Custom answer"] if allow_custom else []),
            )
        result = handler(
            question=normalized_question,
            options=normalized_options,
            allow_custom=bool(allow_custom),
        )
        if not isinstance(result, dict):
            return "ask_user_question failed: invalid question response."
        status = str(result.get("status", "cancelled")).strip().lower()
        payload = {
            "status": "answered" if status == "answered" else "cancelled",
            "question": normalized_question,
            "selected_option": result.get("selected_option"),
            "answer": str(result.get("answer", "")),
            "reason": str(result.get("reason", "")),
        }
        return json.dumps(payload, ensure_ascii=False)

    def set_session_provider_model(
        self,
        session: AgentSession,
        provider_name: str | None,
        model: str | None,
        *,
        reasoning_level: str | None = None,
    ) -> str:
        """Pin one session to a provider/model, or clear the pin to follow the
        workspace default. Only this session's turns are affected.

        ``reasoning_level`` is tri-state: ``None`` leaves the pinned model's
        stored level untouched, ``"auto"``/``"none"``/``""`` clears it, and a
        concrete level (low/medium/high/deep) is written to that model's
        traits and persisted. Clearing the pin takes no reasoning level.
        """
        normalized_provider = str(provider_name or "").strip().lower()
        normalized_model = _normalize_model_id(model)
        if not normalized_provider:
            if reasoning_level is not None:
                raise ValueError("A reasoning level requires a provider/model pin to attach to.")
            session.provider_override = None
            session.model_override = None
            self.session_manager.save(session)
            return f"Session '{session.id}' now follows the workspace default provider/model."
        if normalized_provider not in self.settings.provider_profiles:
            raise ValueError(f"Provider '{normalized_provider}' is not configured.")
        profile = self.settings.provider_profiles[normalized_provider]
        if normalized_model not in profile.models:
            raise ValueError(f"Model '{normalized_model}' is not configured for provider '{normalized_provider}'.")
        # Validate the level before mutating anything, so a rejected call
        # leaves both the pin and the traits untouched.
        normalized_level: str | None = None
        if reasoning_level is not None:
            raw_level = reasoning_level.strip().lower()
            normalized_level = None if raw_level in {"", "auto", "none"} else normalize_reasoning_level(raw_level)
            if raw_level and raw_level not in {"auto", "none"} and normalized_level is None:
                raise ValueError("Reasoning level must be one of: auto, low, medium, high, deep.")
        session.provider_override = normalized_provider
        session.model_override = normalized_model
        self.session_manager.save(session)
        message = f"Session '{session.id}' pinned to provider '{normalized_provider}' with model '{normalized_model}'."
        if reasoning_level is None:
            return message
        model_traits = profile.model_traits.get(normalized_model, ModelTraits())
        model_traits.reasoning_level = normalized_level
        profile.model_traits[normalized_model] = model_traits
        persist_provider_reasoning_level(self.settings, normalized_provider, normalized_model, normalized_level)
        if (
            normalized_provider == str(self.settings.provider.name).strip().lower()
            and normalized_model == _normalize_model_id(self.settings.provider.model)
        ):
            # The pinned pair is the workspace default: refresh the live
            # provider too so status/turns reflect the new level immediately.
            self.settings.provider = _materialize_provider(profile, normalized_model)
            self.provider = self._instantiate_provider(self.settings.provider)
            self.compact_manager.provider = self.provider
            self.compact_manager.model_max_tokens = self.settings.provider.max_tokens
            self._context_usage_cache = {}
            self._payload_message_cache = {}
            self._recent_context_usage = {}
        return f"{message} Reasoning level set to '{normalized_level or 'auto'}'."

    def session_effective_provider(self, session: AgentSession) -> tuple[str, str]:
        """The provider/model a turn of this session will actually use."""
        provider = str(session.provider_override or self.settings.provider.name).strip().lower()
        model = _normalize_model_id(session.model_override or self.settings.provider.model)
        return provider, model

    def switch_provider_model(self, provider_name: str, model: str) -> str:
        normalized_provider = provider_name.strip().lower()
        normalized_model = _normalize_model_id(model)
        if normalized_provider not in self.settings.provider_profiles:
            raise ValueError(f"Provider '{normalized_provider}' is not configured.")
        profile = self.settings.provider_profiles[normalized_provider]
        if normalized_model not in profile.models:
            raise ValueError(f"Model '{normalized_model}' is not configured for provider '{normalized_provider}'.")
        self.settings.provider = _materialize_provider(profile, normalized_model)
        self.settings.provider_profiles[normalized_provider].default_model = normalized_model
        self.provider = self._instantiate_provider(self.settings.provider)
        self.compact_manager.provider = self.provider
        self.compact_manager.model_max_tokens = self.settings.provider.max_tokens
        self._context_usage_cache = {}
        self._payload_message_cache = {}
        self._recent_context_usage = {}
        persist_provider_selection(self.settings, normalized_provider, normalized_model)
        return (
            f"Switched to provider '{self.settings.provider.name}' with model "
            f"'{self.settings.provider.model}' and saved it to .open_somnia/open_somnia.toml."
        )

    def set_reasoning_level(self, reasoning_level: str | None) -> str:
        raw_level = str(reasoning_level or "").strip().lower() if reasoning_level is not None else ""
        clear_requested = reasoning_level is None or raw_level in {"auto", "none"}
        normalized_level = None if clear_requested else normalize_reasoning_level(reasoning_level)
        if not clear_requested and normalized_level is None:
            raise ValueError("Reasoning level must be one of: auto, low, medium, high, deep.")
        provider_name = self.settings.provider.name
        profile = self.settings.provider_profiles.get(provider_name)
        if profile is None:
            raise ValueError(f"Provider '{provider_name}' is not configured.")
        model_name = self.settings.provider.model
        model_traits = profile.model_traits.get(model_name, ModelTraits())
        model_traits.reasoning_level = normalized_level
        profile.model_traits[model_name] = model_traits
        self.settings.provider = _materialize_provider(profile, self.settings.provider.model)
        self.provider = self._instantiate_provider(self.settings.provider)
        self.compact_manager.provider = self.provider
        self.compact_manager.model_max_tokens = self.settings.provider.max_tokens
        self._context_usage_cache = {}
        self._payload_message_cache = {}
        self._recent_context_usage = {}
        persist_provider_reasoning_level(self.settings, provider_name, model_name, normalized_level)
        if clear_requested:
            return (
                f"Set reasoning level for model '{self.settings.provider.model}' to 'auto' "
                "and saved it to .open_somnia/open_somnia.toml."
            )
        return (
            f"Set reasoning level for model '{self.settings.provider.model}' to "
            f"'{normalized_level}' and saved it to .open_somnia/open_somnia.toml."
        )

    def set_vision_model(self, vision_provider: str | None, vision_model: str | None, *, scope: str = "project") -> str:
        normalized_vision_provider = str(vision_provider or "").strip().lower()
        normalized_model = _normalize_model_id(vision_model)
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"user", "project"}:
            raise ValueError("scope must be 'user' or 'project'.")
        if bool(normalized_vision_provider) != bool(normalized_model):
            raise ValueError("vision_provider and vision_model must be set together.")
        if normalized_vision_provider and normalized_vision_provider not in self.settings.provider_profiles:
            raise ValueError(f"Vision provider '{normalized_vision_provider}' is not configured.")
        if normalized_model and normalized_model not in self.settings.provider_profiles[normalized_vision_provider].models:
            raise ValueError(
                f"Vision model '{normalized_model}' is not configured for provider '{normalized_vision_provider}'."
            )
        self.settings.vision_provider = normalized_vision_provider or None
        self.settings.vision_model = normalized_model or None
        self._context_usage_cache = {}
        self._payload_message_cache = {}
        self._recent_context_usage = {}
        persist_vision_model(
            self.settings,
            normalized_vision_provider or None,
            normalized_model or None,
            scope=normalized_scope,
        )
        reloaded = load_settings(
            self.settings.workspace_root,
            provider_override=self.settings.provider.name,
            model_override=self.settings.provider.model,
        )
        self.settings.vision_provider = reloaded.vision_provider
        self.settings.vision_model = reloaded.vision_model
        self.settings.raw_config = reloaded.raw_config
        if normalized_model:
            return (
                f"Set {normalized_scope} shared vision model to "
                f"'{normalized_vision_provider}/{normalized_model}' "
                f"and saved it to {normalized_scope} config."
            )
        return f"Cleared {normalized_scope} shared vision model and saved it to {normalized_scope} config."

    def reload_provider_configuration(self, *, provider_name: str | None = None, model: str | None = None) -> None:
        provider_override = provider_name or self.settings.provider.name
        model_override = model or self.settings.provider.model
        if provider_override not in self.settings.provider_profiles:
            provider_override = None
            model_override = None
        reloaded = load_settings(
            self.settings.workspace_root,
            provider_override=provider_override,
            model_override=model_override,
            allow_missing_provider=True,
        )
        self.settings.provider_profiles = reloaded.provider_profiles
        self.settings.provider = reloaded.provider
        self.settings.vision_provider = reloaded.vision_provider
        self.settings.vision_model = reloaded.vision_model
        self.settings.raw_config = reloaded.raw_config
        self.provider = self._instantiate_provider(self.settings.provider)
        self.compact_manager.provider = self.provider
        self.compact_manager.model_max_tokens = self.settings.provider.max_tokens
        self._context_usage_cache = {}
        self._payload_message_cache = {}
        self._recent_context_usage = {}

    def reload_hook_configuration(self) -> None:
        reloaded = load_settings(
            self.settings.workspace_root,
            provider_override=self.settings.provider.name,
            model_override=self.settings.provider.model,
        )
        self.settings.raw_config = reloaded.raw_config
        self.settings.hooks = reloaded.hooks
        self.hook_manager = HookManager(self.settings)

    def reload_runtime_configuration(self) -> None:
        reloaded = load_settings(
            self.settings.workspace_root,
            provider_override=self.settings.provider.name,
            model_override=self.settings.provider.model,
            allow_missing_provider=True,
        )
        self.settings.raw_config = reloaded.raw_config
        self.settings.runtime = reloaded.runtime
        self.background_manager.default_timeout = reloaded.runtime.command_timeout_seconds
        self.background_manager.max_output_chars = reloaded.runtime.max_tool_output_chars
        self._context_usage_cache = {}
        self._payload_message_cache = {}

    def reload_plugin_configuration(self, *, mcp_registry_factory=None, progress_callback=None) -> dict[str, Any]:
        def emit_progress(message: str) -> None:
            if callable(progress_callback):
                progress_callback(message)

        emit_progress("loading configuration")
        global_raw = _read_toml(global_config_path())
        workspace_raw = _read_toml(workspace_config_path(self.settings.workspace_root))
        raw_config = _merge_config(global_raw, workspace_raw)
        mcp_servers = _load_mcp_servers(self.settings.workspace_root, raw_config)
        old_registry = getattr(self, "mcp_registry", None)
        if old_registry is not None:
            emit_progress("closing existing MCP clients")
            old_registry.close()
        self.settings.raw_config = raw_config
        self.settings.mcp_servers = mcp_servers
        registry_factory = mcp_registry_factory or MCPRegistry
        emit_progress("registering MCP tools")
        self.mcp_registry = registry_factory(self.settings.mcp_servers)
        self.registry = ToolRegistry()
        self._register_core_tools(self.registry)
        emit_progress("reloading skills")
        self.skill_loader = SkillLoader.for_workspace(self.settings.workspace_root)
        self.skill_loader.reload()
        emit_progress("reloading project instructions")
        self.system_prompt_builder = SystemPromptBuilder(self)
        self.invalidate_tool_schema_state()
        project_instructions = ProjectInstructionsLoader(self.settings.workspace_root).load_scoped()
        enabled_mcp_servers = [server for server in self.settings.mcp_servers if getattr(server, "enabled", True)]
        server_tools = getattr(self.mcp_registry, "server_tools", {})
        mcp_tool_count = sum(len(tools) for tools in server_tools.values()) if isinstance(server_tools, dict) else 0
        return {
            "mcp_server_count": len(enabled_mcp_servers),
            "mcp_tool_count": mcp_tool_count,
            "skill_count": len(getattr(self.skill_loader, "skills", {}) or {}),
            "project_instruction_count": len(project_instructions),
            "mcp_errors": dict(getattr(self.mcp_registry, "errors", {}) or {}),
            "tool_warnings": list(getattr(self.registry, "registration_warnings", []) or []),
            "mcp_warnings": list(getattr(self.mcp_registry, "warnings", []) or []),
        }

    def set_hook_enabled(self, hook: HookSettings, enabled: bool) -> str:
        config_path = persist_hook_enabled(hook, enabled)
        self.reload_hook_configuration()
        state = "enabled" if enabled else "disabled"
        kind = "builtin" if hook.managed_by == BUILTIN_NOTIFY_MANAGER else "custom"
        scope = getattr(hook, "config_scope", None) or "config"
        return f"{state.capitalize()} {kind} hook for {hook.event} in {scope} config: {config_path}"

    def set_mcp_tool_enabled(self, server_name: str, tool_name: str, enabled: bool) -> dict[str, Any]:
        """Persist one MCP tool's enabled state and reload the plugin config so
        the registry reflects it. Shared by the CLI /mcp command and the
        desktop sidecar endpoint."""
        config_path = persist_mcp_tool_enabled(self.settings.workspace_root, server_name, tool_name, enabled)
        summary = self.reload_plugin_configuration()
        registry = getattr(self, "mcp_registry", None)
        tool_summaries = registry.tool_summaries(server_name) if registry is not None else []
        tool_summary = next((item for item in tool_summaries if item.get("name") == tool_name), None)
        return {
            "server": server_name,
            "tool": tool_name,
            "enabled": enabled,
            "config_path": str(config_path),
            "tool_summary": tool_summary,
            "enabled_tool_count": sum(1 for item in tool_summaries if item.get("enabled")),
            "tool_count": len(tool_summaries),
            "reload": summary,
        }

    def _augment_tool_schemas_with_importance(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        augmented: list[dict[str, Any]] = []
        for schema in schemas:
            clone = deepcopy(schema)
            input_schema = clone.get("input_schema")
            if isinstance(input_schema, dict) and input_schema.get("type") == "object":
                properties = input_schema.setdefault("properties", {})
                if "importance" not in properties:
                    properties["importance"] = {
                        "type": "string",
                        "enum": list(self.TOOL_IMPORTANCE_VALUES),
                        "description": (
                            "Optional context-governance hint. "
                            "Use 'glance' for disposable checks, 'investigate' for normal exploration, "
                            "or 'foundation' for evidence that should be preserved more strongly."
                        ),
                    }
            augmented.append(clone)
        return augmented

    def _tool_schemas_for_model(self, actor: str) -> list[dict[str, Any]]:
        registry = self.registry if actor == "lead" else self.worker_registry
        return sorted(
            self._augment_tool_schemas_with_importance(registry.schemas()),
            key=lambda schema: (
                str(schema.get("name", "")),
                json.dumps(schema, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )

    def invalidate_tool_schema_state(self) -> None:
        self._context_usage_cache = {}
        self._payload_message_cache = {}
        self._recent_context_usage = {}

    def _context_usage_tools(self, actor: str) -> list[dict[str, Any]]:
        return self._tool_schemas_for_model(actor)

    def _tool_importance_preservation_score(self, importance: str | None) -> int:
        normalized = str(importance or "").strip().lower()
        if normalized == "foundation":
            return 4
        if normalized == "investigate":
            return 1
        if normalized == "glance":
            return -2
        return 0

    def _context_usage_cache_key(
        self,
        session: AgentSession,
        *,
        actor: str,
        role: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> tuple[Any, ...]:
        messages = getattr(session, "messages", None)
        if not isinstance(messages, list):
            messages = []
        last_message = messages[-1] if messages else None
        try:
            last_message_digest = (
                json.dumps(last_message, ensure_ascii=False, sort_keys=True, default=str) if last_message is not None else ""
            )
        except Exception:
            last_message_digest = str(last_message)
        return (
            id(messages),
            len(messages),
            getattr(session, "latest_turn_id", None),
            last_message_digest,
            actor,
            role,
            system_prompt,
            tuple(str(tool.get("name", "")) for tool in tools),
            getattr(self.settings.provider, "name", ""),
            getattr(self.settings.provider, "model", ""),
            getattr(self, "execution_mode", DEFAULT_EXECUTION_MODE),
        )

    def _count_payload_usage(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ContextWindowUsage:
        provider = getattr(self, "provider", None)
        counter_name = "estimate"
        try:
            if provider is not None and callable(getattr(provider, "count_tokens", None)):
                used_tokens = int(provider.count_tokens(system_prompt, messages, tools))
                if used_tokens <= 0 and (system_prompt.strip() or messages or tools):
                    raise ValueError("Provider token counter returned a non-positive token count for a non-empty payload.")
                counter_name = str(provider.token_counter_name())
            else:
                raise RuntimeError("Provider token counting unavailable.")
        except Exception:
            used_tokens = estimate_payload_tokens(system_prompt, messages, tools)

        context_window_tokens = None
        if provider is not None and callable(getattr(provider, "context_window_tokens", None)):
            context_window_tokens = provider.context_window_tokens()
        if context_window_tokens is None:
            context_window_tokens = getattr(getattr(self.settings, "provider", None), "context_window_tokens", None)
        return ContextWindowUsage(
            used_tokens=used_tokens,
            max_tokens=int(context_window_tokens) if context_window_tokens is not None else None,
            counter_name=counter_name,
        )

    def _build_payload_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        session: AgentSession | None = None,
    ) -> list[dict[str, Any]]:
        return build_payload_messages(
            messages,
            preserve_thinking_blocks=self._preserve_provider_thinking_blocks(),
        )

    def _runtime_notice_message(self, notices: list[str]) -> dict[str, Any] | None:
        lines = [str(notice).strip() for notice in notices if str(notice).strip()]
        if not lines:
            return None
        if len(lines) == 1:
            content = f"{self.RUNTIME_NOTICE_PREFIX} {lines[0]}"
        else:
            body = "\n".join(f"- {line}" for line in lines)
            content = f"{self.RUNTIME_NOTICE_PREFIX}\n{body}"
        message = make_user_text_message(content)
        message["transient"] = True
        return message

    def _preserve_provider_thinking_blocks(self) -> bool:
        settings = getattr(self, "settings", None)
        provider_type = str(getattr(getattr(settings, "provider", None), "provider_type", "") or "").strip().lower()
        return provider_type == "anthropic"

    def _payload_message_cache_key(
        self,
        session: AgentSession,
        *,
        actor: str,
        role: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> tuple[Any, ...]:
        return self._context_usage_cache_key(
            session,
            actor=actor,
            role=role,
            system_prompt=system_prompt,
            tools=tools,
        )

    def _consume_ephemeral_image_history(
        self,
        messages: list[dict[str, Any]],
        *,
        session_id: str | None = None,
    ) -> bool:
        changed = consume_ephemeral_image_blocks(messages)
        if not changed:
            return False
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return True
        payload_cache = getattr(self, "_payload_message_cache", None)
        if isinstance(payload_cache, dict):
            payload_cache.pop(normalized_session_id, None)
        usage_cache = getattr(self, "_context_usage_cache", None)
        if isinstance(usage_cache, dict):
            usage_cache.pop(normalized_session_id, None)
        recent_usage = getattr(self, "_recent_context_usage", None)
        if isinstance(recent_usage, dict):
            recent_usage.pop(normalized_session_id, None)
        return True

    def _note_context_governance(self, session_id: str, kind: str, label: str) -> None:
        events = getattr(self, "_context_governance_events", None)
        if events is None:
            events = {}
            self._context_governance_events = events
        events[str(session_id)] = {
            "kind": str(kind).strip().lower(),
            "label": str(label).strip(),
            "changed_at": time.monotonic(),
        }

    def _context_pressure_bands(self) -> dict[str, str]:
        # Last pressure band announced per session ("" = below soft). Stored
        # via __dict__ so runtimes built without __init__ (tests) still work;
        # refreshed unconditionally every round, so it self-corrects after a
        # compact drops usage back into the healthy band.
        return self.__dict__.setdefault("_context_pressure_bands_map", {})

    def _maybe_append_context_pressure_message(self, session: AgentSession) -> None:
        """Early-warning companion to auto-compaction.

        Auto-compact (0.82) is lossy and fires mid-work at a round boundary.
        Before that, once per pressure episode, append a *persisted* user
        message — not a transient notice — so every later round still sees it
        and the compaction summary inherits it: finish the current stage,
        externalize state, hand off via request_new_session.
        """
        try:
            usage = self.context_window_usage(session)
        except Exception:
            return
        runtime_settings = getattr(self.settings, "runtime", None)
        level = context_pressure_level(
            usage,
            soft_ratio=getattr(runtime_settings, "context_pressure_soft_ratio", None),
            urgent_ratio=getattr(runtime_settings, "context_pressure_urgent_ratio", None),
        )
        bands = self._context_pressure_bands()
        session_key = str(session.id)
        if level is None:
            bands[session_key] = ""
            return
        rank = {"": 0, "soft": 1, "urgent": 2}
        if rank[level] > rank.get(bands.get(session_key, ""), 0):
            message = make_user_text_message(self._render_context_pressure_message(level, usage))
            session.messages.append(message)
            try:
                self._append_transcript_entry(session.id, message)
            except Exception:
                pass
        bands[session_key] = level

    def _render_context_pressure_message(self, level: str, usage: ContextWindowUsage) -> str:
        template = (
            self.CONTEXT_PRESSURE_URGENT_MESSAGE_TEMPLATE
            if level == "urgent"
            else self.CONTEXT_PRESSURE_SOFT_MESSAGE_TEMPLATE
        )
        ratio = usage.usage_ratio or 0.0
        return template.format(
            percent=f"{ratio * 100:.0f}",
            used=f"{usage.used_tokens:,}",
            max=f"{usage.max_tokens:,}" if usage.max_tokens else "unknown",
            counter=usage.counter_name,
            trigger=f"{AUTO_COMPACT_TRIGGER_RATIO * 100:.0f}%",
        )

    def _provider_payload_dump_enabled(self) -> bool:
        raw = str(os.environ.get(self.DEBUG_PROVIDER_PAYLOAD_ENV, "")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _tool_schema_summary(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, int] = {}
        for tool in tools:
            name = str(tool.get("name", "")).strip()
            group = self._tool_schema_group(name)
            groups[group] = groups.get(group, 0) + 1
        return {
            "total": len(tools),
            "groups": dict(sorted(groups.items())),
        }

    def _message_payload_summary(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        roles: dict[str, int] = {}
        chars = 0
        for message in messages:
            role = str(message.get("role", "unknown"))
            roles[role] = roles.get(role, 0) + 1
            chars += len(json.dumps(message.get("content", ""), ensure_ascii=False, default=str))
        return {
            "total": len(messages),
            "roles": dict(sorted(roles.items())),
            "content_chars": chars,
        }

    def _tool_schema_group(self, name: str) -> str:
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            server = parts[1] if len(parts) > 1 and parts[1] else "unknown"
            return f"mcp:{server}"
        if name in {AUTHORIZATION_TOOL_NAME, MODE_SWITCH_TOOL_NAME}:
            return "core"
        if name == "load_skill":
            return "skill"
        if name in {"TodoWrite"} or name.startswith("task_"):
            return "task"
        if name in {"bash", "tree", "glob", "grep", "read_file", "read_image", "write_file", "edit_file", "find_symbol"}:
            return "filesystem"
        if name.startswith("background_"):
            return "background"
        if name.startswith("teammate_") or name.startswith("inbox_") or name.startswith("team_"):
            return "team"
        return "other"

    def _is_exploration_tool_call(self, name: str, payload: dict[str, Any] | None = None) -> bool:
        tool_name = str(name or "").strip()
        normalized = tool_name.lower()
        if normalized in self.EXPLORATION_TOOL_NAMES:
            return True
        if normalized.startswith("mcp__gitnexus__"):
            parts = normalized.split("__", 2)
            gitnexus_tool = parts[2] if len(parts) > 2 else ""
            return gitnexus_tool in self.EXPLORATION_GITNEXUS_TOOL_NAMES
        if normalized != "bash":
            return False
        command = str((payload or {}).get("command", "")).strip().lower()
        return any(command == prefix.rstrip() or command.startswith(prefix) for prefix in self.EXPLORATION_SHELL_PREFIXES)

    def _parallel_safe_kind(self, tool_call: Any) -> str:
        """Which dispatch pool a parallel-safe call routes to: ``tool`` or ``subagent``.

        Read-only tools go to ``_POOL`` (``dispatch_parallel_segment``); an
        Explore-subagent call goes to ``_SUBAGENT_POOL``
        (``run_parallel_explore_subagents``). Segment construction bounds a
        maximal run by kind so the two never mix in one concurrent segment
        (they return different shapes and use different pools).
        """
        return "subagent" if is_explore_subagent_safe(tool_call) else "tool"

    def _print_tool_finished_subagent(self, tool_call: Any) -> None:
        """Notify the host that a parallel Explore-subagent call finished.

        The parallel subagent path pre-fires ``print_tool_started`` before
        dispatch and bypasses ``registry.execute`` (whose wrapper would otherwise
        emit the finished signal), so the lead loop calls this in the
        post-completion side-effect loop to let the host clear the
        active-subagent slot keyed by ``tool_call.id``.
        """
        self.print_tool_finished(
            "lead",
            "subagent",
            getattr(tool_call, "input", {}) or {},
            tool_call_id=self._subagent_slot_id(tool_call),
        )

    def _subagent_slot_id(self, tool_call: Any) -> str | None:
        """Resolve the UI active-subagent slot key for a subagent tool_call.

        A fresh subagent reports activity under ``tool_call.id`` (the id the
        lead pre-fires). A RESUMED subagent, however, runs under its
        checkpoint's activity_id (``resume_from``), so the resumed subagent's
        internal activity events carry that id, not the new tool_call.id. If
        the lead pre-fired / finishes keyed by ``tool_call.id`` while the
        subagent reports under ``resume_from``, the two never match and the
        host opens a SECOND slot for the resumed subagent -- doubling the
        displayed count on resume (a fresh pre-fire slot + a self-reported
        slot). Keying the host events on ``resume_from`` when present keeps
        one slot per resumed subagent and lets the matching finish clear it.
        """
        tool_input = getattr(tool_call, "input", None)
        if isinstance(tool_input, dict):
            resume_from = str(tool_input.get("resume_from") or "").strip()
            if resume_from:
                return resume_from
        return getattr(tool_call, "id", None)

    def _exploration_summary_reminder(self, *, streak: int, total: int) -> str:
        return self.EXPLORATION_SUMMARY_REMINDER_TEXT.format(streak=streak, total=total)

    def _assistant_message_has_thinking_log(self, message: dict[str, Any]) -> bool:
        content = message.get("content")
        if not isinstance(content, list):
            return False
        for item in content:
            if isinstance(item, dict) and str(item.get("type", "")).strip() == "thinking_log":
                return True
        return False

    def _dump_provider_payload_if_enabled(
        self,
        *,
        session: AgentSession,
        system_prompt: str,
        payload_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        provider: LLMProvider | None = None,
        actor: str = "lead",
        stream: bool = False,
        kind: str = "turn",
    ) -> Path | None:
        if not self._provider_payload_dump_enabled():
            return None
        storage = getattr(self.settings, "storage", None)
        logs_dir = getattr(storage, "logs_dir", None)
        if not logs_dir:
            return None
        logs_root = Path(logs_dir)
        provider = provider or getattr(self, "provider", None)
        usage = self._count_payload_usage(system_prompt, payload_messages, tools)
        provider_payload: dict[str, Any] | None = None
        serializer = getattr(provider, "debug_request_payload", None)
        if callable(serializer):
            try:
                provider_payload = serializer(
                    system_prompt,
                    payload_messages,
                    tools,
                    max_tokens,
                    stream=stream,
                )
            except Exception as exc:
                provider_payload = {"error": f"failed to serialize provider payload: {exc}"}
        system_prompt_sections: list[dict[str, object]] | None = None
        section_builder = getattr(self, "build_system_prompt_sections", None)
        if callable(section_builder) and kind == "turn":
            try:
                system_prompt_sections = section_builder(actor=actor, session=session)
            except Exception as exc:
                system_prompt_sections = [
                    {
                        "id": "error",
                        "title": "System Prompt Sections",
                        "dynamic": True,
                        "content": f"failed to build system prompt sections: {exc}",
                    }
                ]
        tool_schema_summary = self._tool_schema_summary(tools)
        message_summary = self._message_payload_summary(payload_messages)
        payload_summary = {
            "kind": str(kind).strip().lower() or "turn",
            "system_prompt_chars": len(system_prompt),
            "system_prompt_section_count": len(system_prompt_sections or []),
            "message_count": len(payload_messages),
            "message_content_chars": message_summary["content_chars"],
            "tool_count": len(tools),
            "max_tokens": max_tokens,
            "stream": stream,
        }
        dump_payload = {
            "timestamp": time.time(),
            "session_id": str(getattr(session, "id", "")).strip(),
            "actor": actor,
            "kind": str(kind).strip().lower() or "turn",
            "provider": {
                "name": getattr(getattr(provider, "settings", None), "name", getattr(getattr(self.settings, "provider", None), "name", "")),
                "type": getattr(
                    getattr(provider, "settings", None),
                    "provider_type",
                    getattr(getattr(self.settings, "provider", None), "provider_type", ""),
                ),
                "model": getattr(getattr(provider, "settings", None), "model", getattr(getattr(self.settings, "provider", None), "model", "")),
                "base_url": getattr(
                    getattr(provider, "settings", None),
                    "base_url",
                    getattr(getattr(self.settings, "provider", None), "base_url", None),
                ),
            },
            "context_usage": {
                "used_tokens": usage.used_tokens,
                "max_tokens": usage.max_tokens,
                "usage_ratio": usage.usage_ratio,
                "usage_percent": usage.usage_percent,
                "counter_name": usage.counter_name,
            },
            "system_prompt_sections": system_prompt_sections,
            "messages": payload_messages,
            "message_summary": message_summary,
            "tools": tools,
            "tool_schema_summary": tool_schema_summary,
            "payload_summary": payload_summary,
            "max_tokens": max_tokens,
            "stream": stream,
            "provider_request": provider_payload,
            "provider_response": None,
            "response_text": None,
            "provider_error": None,
            "latency_ms": None,
            "session_path": str(getattr(storage, "sessions_dir", "") / f"{session.id}.json") if getattr(storage, "sessions_dir", None) else None,
            "transcript_path": str(self.transcript_store.transcript_path(session.id)) if getattr(self, "transcript_store", None) else None,
        }
        dump_dir = logs_root / "provider_payloads"
        dump_name = f"{session.id}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        dump_path = dump_dir / dump_name
        atomic_write_text(dump_path, json.dumps(dump_payload, ensure_ascii=False, indent=2, default=str))
        return dump_path

    def _serialize_provider_response(self, turn: Any) -> dict[str, Any]:
        tool_calls: list[dict[str, Any]] = []
        for tool_call in list(getattr(turn, "tool_calls", []) or []):
            tool_calls.append(
                {
                    "id": getattr(tool_call, "id", None),
                    "name": getattr(tool_call, "name", None),
                    "input": getattr(tool_call, "input", None),
                }
            )
        text_blocks = [str(block) for block in list(getattr(turn, "text_blocks", []) or [])]
        return {
            "stop_reason": getattr(turn, "stop_reason", None),
            "text_blocks": text_blocks,
            "tool_calls": tool_calls,
            "usage": getattr(turn, "usage", None),
            "raw_response": getattr(turn, "raw_response", None),
        }

    def _record_provider_payload_result(
        self,
        dump_path: Path | None,
        *,
        turn: Any | None = None,
        error: BaseException | None = None,
        latency_ms: float | None = None,
    ) -> None:
        if dump_path is None:
            return
        try:
            payload = json.loads(Path(dump_path).read_text(encoding="utf-8"))
        except Exception:
            return
        if latency_ms is not None:
            payload["latency_ms"] = round(max(0.0, float(latency_ms)), 3)
        if turn is not None:
            response = self._serialize_provider_response(turn)
            payload["provider_response"] = response
            payload["response_text"] = "\n\n".join(response.get("text_blocks") or []).strip()
            payload["provider_error"] = None
        if error is not None:
            payload["provider_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            retryable = getattr(error, "retryable", None)
            if retryable is not None:
                payload["provider_error"]["retryable"] = bool(retryable)
        atomic_write_text(Path(dump_path), json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def recent_context_governance_label(self, session, *, max_age_seconds: float = 15.0) -> str:
        session_id = str(getattr(session, "id", "")).strip()
        if not session_id:
            return ""
        events = getattr(self, "_context_governance_events", None) or {}
        entry = events.get(session_id)
        if not isinstance(entry, dict):
            return ""
        label = str(entry.get("label", "")).strip()
        changed_at = float(entry.get("changed_at", 0.0) or 0.0)
        if not label:
            return ""
        if max_age_seconds > 0 and changed_at > 0 and (time.monotonic() - changed_at) > max_age_seconds:
            return ""
        return label

    def _remember_context_usage(self, session_id: str, usage: ContextWindowUsage | None) -> None:
        if not session_id or not isinstance(usage, ContextWindowUsage):
            return
        cache = getattr(self, "_recent_context_usage", None)
        if cache is None:
            cache = {}
            self._recent_context_usage = cache
        cache[str(session_id)] = usage

    def recent_context_window_usage(self, session: AgentSession) -> ContextWindowUsage | None:
        session_id = str(getattr(session, "id", "")).strip()
        if not session_id:
            return None
        cache = getattr(self, "_recent_context_usage", None) or {}
        usage = cache.get(session_id)
        if isinstance(usage, ContextWindowUsage):
            return usage
        cached_usage = (getattr(self, "_context_usage_cache", None) or {}).get(session_id)
        if isinstance(cached_usage, tuple) and len(cached_usage) == 2 and isinstance(cached_usage[1], ContextWindowUsage):
            self._remember_context_usage(session_id, cached_usage[1])
            return cached_usage[1]
        return None

    def _messages_for_model(
        self,
        messages: list[dict[str, Any]],
        *,
        session: AgentSession | None = None,
        actor: str = "lead",
        role: str = "lead coding agent",
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if session is None:
            return build_payload_messages(
                messages,
                preserve_thinking_blocks=self._preserve_provider_thinking_blocks(),
            )
        if system_prompt is None:
            try:
                system_prompt = self.build_system_prompt(actor=actor, role=role, session=session)
            except TypeError:
                system_prompt = self.build_system_prompt()
        if tools is None:
            tools = self._context_usage_tools(actor)

        cache_key = self._payload_message_cache_key(
            session,
            actor=actor,
            role=role,
            system_prompt=system_prompt,
            tools=tools,
        )
        cache = getattr(self, "_payload_message_cache", None)
        if cache is None:
            cache = {}
            self._payload_message_cache = cache
        usage_cache = getattr(self, "_context_usage_cache", None)
        if usage_cache is None:
            usage_cache = {}
            self._context_usage_cache = usage_cache
        cached = cache.get(session.id)
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        payload_messages = self._build_payload_messages(messages, session=session)
        cache[session.id] = (cache_key, payload_messages)
        return payload_messages

    def _estimate_completion_output_tokens(self, turn) -> int:
        try:
            assistant_message = turn.as_message()
        except Exception:
            text = "\n".join(getattr(turn, "text_blocks", []) or [])
            return max(0, estimate_payload_tokens("", [{"role": "assistant", "content": text}], []))
        return max(0, estimate_payload_tokens("", [assistant_message], []))

    def _normalize_turn_usage(
        self,
        turn,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, int | str]:
        usage = getattr(turn, "usage", None)
        if isinstance(usage, dict):
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
            cache_read_input_tokens = int(usage.get("cache_read_input_tokens") or 0)
            cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens") or 0)
            source = str(usage.get("source", "provider"))
            if total_tokens > 0:
                normalized: dict[str, int | str] = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "source": source,
                }
                reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
                if reasoning_tokens > 0:
                    normalized["reasoning_tokens"] = reasoning_tokens
                return normalized

        provider = getattr(self, "provider", None)
        try:
            if provider is not None and callable(getattr(provider, "count_tokens", None)):
                input_tokens = int(provider.count_tokens(system_prompt, messages, tools))
            else:
                raise RuntimeError("Provider token counting unavailable.")
        except Exception:
            input_tokens = estimate_payload_tokens(system_prompt, messages, tools)
        output_tokens = self._estimate_completion_output_tokens(turn)
        return {
            "input_tokens": max(0, input_tokens),
            "output_tokens": max(0, output_tokens),
            "total_tokens": max(0, input_tokens + output_tokens),
            "source": "estimate",
        }

    def _detect_reasoning_budget_exhaustion(self, turn, *, current_max_tokens: int) -> bool:
        """Return True when a turn spent essentially the whole max_tokens budget on reasoning.

        This is the signature of the OpenAI-compatible reasoning path where max_tokens
        is a single shared budget: a reasoning model can burn it all on reasoning_content
        and emit no visible text or tool calls, then get truncated mid-thought. Detecting
        it lets the loop raise the budget once before giving up.
        """
        if current_max_tokens <= 0:
            return False
        usage = getattr(turn, "usage", None)
        if not isinstance(usage, dict):
            return False
        output_tokens = int(usage.get("output_tokens") or 0)
        if output_tokens <= 0:
            return False
        # Prefer the explicit reasoning_tokens breakdown when the provider reports it
        # (OpenAI chat-completions completion_tokens_details.reasoning_tokens).
        reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
        if reasoning_tokens > 0:
            return reasoning_tokens >= int(current_max_tokens * self.EMPTY_RESPONSE_REASONING_BUDGET_RATIO)
        # Fall back to: all output tokens consumed and close to the max_tokens ceiling.
        # output_tokens == current_max_tokens with no visible text is a strong signal.
        return output_tokens >= int(current_max_tokens * self.EMPTY_RESPONSE_REASONING_BUDGET_RATIO)

    def _compute_reasoning_budget_bump(self, current_max_tokens: int) -> int | None:
        """Compute a clamped, larger max_tokens for a one-shot reasoning-exhaustion retry.

        Returns None if no safe bump is possible (no context window known, or already
        at/above the ceiling). The result is always clamped to the model's context
        window so we never request more than the model can accept.
        """
        if current_max_tokens <= 0:
            return None
        target = int(current_max_tokens * self.EMPTY_RESPONSE_REASONING_BUMP_FACTOR)
        target = min(target, self.EMPTY_RESPONSE_REASONING_BUMP_MAX_TOKENS)
        if target <= current_max_tokens:
            return None
        context_window = self._resolved_context_window_tokens()
        if context_window is not None and context_window > 0:
            # Reserve headroom for input; never let max_tokens eat the whole window.
            cap = max(current_max_tokens + 1, int(context_window * 0.75))
            target = min(target, cap)
        # Require a meaningful bump (at least ~20% larger); a +1 token nudge from
        # a tight context-window clamp is useless and would just re-trip the loop.
        minimum_useful = int(current_max_tokens * 1.2)
        if target < minimum_useful:
            return None
        return target

    def _maybe_raise_reasoning_budget(self, turn) -> int | None:
        """If a thinking-only turn exhausted the budget on reasoning, raise it once.

        Returns the new max_tokens when a bump was staged, or None when no bump
        was possible (no usage data, not a reasoning-exhaustion signature, or the
        configured budget is already at the context-window ceiling). The bump is
        staged as a transient override consumed by complete() on the next call.
        """
        current = getattr(self.settings.provider, "max_tokens", 0)
        try:
            current = int(current)
        except (TypeError, ValueError):
            current = 0
        if not self._detect_reasoning_budget_exhaustion(turn, current_max_tokens=current):
            return None
        # Don't stack bumps: only raise from the configured baseline.
        if getattr(self, "_transient_max_tokens_override", None) is not None:
            return None
        target = self._compute_reasoning_budget_bump(current)
        if target is None:
            return None
        self._set_transient_max_tokens_override(target)
        return target

    def _resolved_context_window_tokens(self) -> int | None:
        provider = getattr(self, "provider", None)
        value: int | None = None
        if provider is not None and callable(getattr(provider, "context_window_tokens", None)):
            try:
                value = provider.context_window_tokens()
            except Exception:
                value = None
        if value is None:
            value = getattr(getattr(self.settings, "provider", None), "context_window_tokens", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _set_transient_max_tokens_override(self, value: int | None) -> None:
        """Stage a one-shot max_tokens bump for the next complete() call.

        Used by the agent loop to recover from a reasoning-budget exhaustion:
        the next completion runs with the larger budget, then the override is
        cleared so subsequent turns revert to the configured value.
        """
        try:
            self._transient_max_tokens_override = int(value) if value is not None else None
        except (TypeError, ValueError):
            self._transient_max_tokens_override = None

    def _consume_transient_max_tokens_override(self, fallback: int) -> int:
        override = getattr(self, "_transient_max_tokens_override", None)
        if override is None:
            try:
                return int(fallback)
            except (TypeError, ValueError):
                return fallback
        self._transient_max_tokens_override = None
        return int(override)

    def _ensure_session_token_usage(self, session: AgentSession) -> dict[str, int]:
        usage = getattr(session, "token_usage", None)
        if not isinstance(usage, dict):
            usage = {}
            session.token_usage = usage
        for key in ("input_tokens", "output_tokens", "total_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            usage[key] = int(usage.get(key) or 0)
        return usage

    def _record_session_token_usage(self, session: AgentSession, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        totals = self._ensure_session_token_usage(session)
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)
        totals["cache_read_input_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
        totals["cache_creation_input_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)

    def context_window_usage(
        self,
        session: AgentSession,
        *,
        actor: str = "lead",
        role: str = "lead coding agent",
    ) -> ContextWindowUsage:
        messages = getattr(session, "messages", None)
        if not isinstance(messages, list):
            messages = []
        try:
            system_prompt = self.build_system_prompt(actor=actor, role=role, session=session)
        except TypeError:
            system_prompt = self.build_system_prompt()
        tools = self._context_usage_tools(actor)
        payload_messages = self._messages_for_model(
            messages,
            session=session,
            actor=actor,
            role=role,
            system_prompt=system_prompt,
            tools=tools,
        )
        cache_key = self._context_usage_cache_key(
            session,
            actor=actor,
            role=role,
            system_prompt=system_prompt,
            tools=tools,
        )
        cache = getattr(self, "_context_usage_cache", None)
        if cache is None:
            cache = {}
            self._context_usage_cache = cache
        cached = cache.get(session.id)
        if cached is not None and cached[0] == cache_key:
            self._remember_context_usage(session.id, cached[1])
            return cached[1]
        usage = self._count_payload_usage(system_prompt, payload_messages, tools)
        cache[session.id] = (cache_key, usage)
        self._remember_context_usage(session.id, usage)
        return usage

    def _context_compact_text(self, text: str, *, limit: int = 220) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 3)] + "..."

    def _candidate_relevance_score(
        self,
        candidate: ToolResultCandidate,
        *,
        active_files: set[str],
        active_symbols: set[str],
        topic_tokens: set[str],
        open_todo_tokens: set[str],
        completed_todo_tokens: set[str],
    ) -> int:
        haystack = self._tool_candidate_haystack(candidate)
        score = 0
        if candidate.has_error:
            score += 5
        if any(file_name.lower() in haystack for file_name in active_files):
            score += 3
        symbol_hits = sum(1 for symbol in active_symbols if symbol.lower() in haystack)
        score += min(symbol_hits, 3) * 2
        topic_hits = sum(1 for token in topic_tokens if token in haystack)
        score += min(topic_hits, 3)
        open_hits = sum(1 for token in open_todo_tokens if token in haystack)
        score += min(open_hits, 2)
        completed_hits = sum(1 for token in completed_todo_tokens if token in haystack)
        if completed_hits and topic_hits == 0 and symbol_hits == 0:
            score -= 1
        score += self._tool_importance_preservation_score(candidate.importance)
        if candidate.tool_name in {"read_file", "find_symbol", "read_text"}:
            score += 2
        if candidate.tool_name in {"pwd", "cd", "ls", "tree", "glob"}:
            score -= 3
        if candidate.age >= 6:
            score -= 1
        if candidate.age >= 10:
            score -= 1
        return score

    def _register_core_tools(self, registry: ToolRegistry) -> None:
        register_shell_tool(registry)
        register_filesystem_tools(registry)
        register_web_fetch_tool(registry)
        register_todo_tool(registry, self.todo_manager)
        register_task_tools(registry, self.task_store)
        register_subagent_tool(registry)
        register_background_tools(registry, self.background_manager)
        register_team_tools(registry, self.team_manager, self.bus, self.request_tracker)
        self._register_local_tools(registry)
        register_mcp_tools(registry, self.mcp_registry)

    def register_worker_tools(self, registry: ToolRegistry) -> None:
        register_shell_tool(registry)
        register_filesystem_tools(registry)
        register_web_fetch_tool(registry)
        register_task_tools(registry, self.task_store, allow_dep_removal=False)
        self._register_worker_local_tools(registry)

    def _register_local_tools(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolDefinition(
                name="load_skill",
                description="Load specialized knowledge by skill name.",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                handler=lambda ctx, payload: self.skill_loader.load(payload["name"]),
            )
        )
        registry.register(
            ToolDefinition(
                name=AUTHORIZATION_TOOL_NAME,
                description=(
                    "Request user approval for a blocked tool call. "
                    "Use this before edits in read-only modes or before broader tools in accept-edits mode."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string"},
                        "reason": {"type": "string"},
                        "argument_summary": {"type": "string"},
                    },
                    "required": ["tool_name", "reason"],
                },
                handler=lambda ctx, payload: self.request_authorization(
                    payload["tool_name"],
                    payload["reason"],
                    payload.get("argument_summary", ""),
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name=MODE_SWITCH_TOOL_NAME,
                description=(
                    "Request that the user switch execution mode to shortcuts or accept_edits only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "target_mode": {"type": "string", "enum": list(NON_YOLO_EXECUTION_MODES)},
                        "reason": {"type": "string"},
                    },
                    "required": ["target_mode"],
                },
                handler=lambda ctx, payload: self.request_mode_switch(payload["target_mode"], payload.get("reason", "")),
            )
        )
        registry.register(
            ToolDefinition(
                name=ASK_USER_QUESTION_TOOL_NAME,
                description=(
                    "Ask the user a question with selectable options. "
                    "The user may pick one of the options or provide a custom answer."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "allow_custom": {"type": "boolean"},
                    },
                    "required": ["question", "options"],
                },
                handler=lambda ctx, payload: self.ask_user_question(
                    payload["question"],
                    payload.get("options") or [],
                    payload.get("allow_custom", True),
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name=NEW_SESSION_TOOL_NAME,
                description=(
                    "End the current turn and start a fresh session, optionally passing handoff "
                    "text that becomes the new session's first prompt. Use at task/phase boundaries "
                    "when the remaining context is disposable. The previous session is preserved "
                    "and stays resumable; the provider/model selection carries over. The task board "
                    "carries over too — the fresh session keeps reading and claiming the same tasks."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"handoff": {"type": "string"}},
                },
                handler=lambda ctx, payload: self.request_new_session(
                    payload.get("handoff", ""),
                    session_id=str(getattr(getattr(ctx, "session", None), "id", "") or ""),
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name="compress",
                description="Manually compact the current conversation context.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: "Compressing...",
            )
        )

    def _register_worker_local_tools(self, registry: ToolRegistry) -> None:
        def worker_session_id(actor: str) -> str | None:
            getter = getattr(getattr(self, "team_manager", None), "_member_session_id", None)
            if callable(getter):
                return getter(actor)
            return None

        def worker_request_authorization(ctx, payload: dict[str, Any]) -> str:
            return self._request_worker_authorization(
                ctx.actor,
                payload["tool_name"],
                payload["reason"],
                payload.get("argument_summary", ""),
                should_interrupt=getattr(ctx, "should_interrupt", None),
            )

        registry.register(
            ToolDefinition(
                name="send_message",
                description="Send a message to another teammate or lead.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["to", "content"],
                },
                handler=lambda ctx, payload: self.bus.send(
                    ctx.actor,
                    payload["to"],
                    payload["content"],
                    session_id=worker_session_id(ctx.actor),
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name="idle",
                description="Enter idle state.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: "Entering idle phase.",
            )
        )
        registry.register(
            ToolDefinition(
                name="submit_plan",
                description="Submit a plan for lead approval.",
                input_schema={
                    "type": "object",
                    "properties": {"plan": {"type": "string"}},
                    "required": ["plan"],
                },
                handler=lambda ctx, payload: self._submit_plan(ctx.actor, payload["plan"]),
            )
        )
        registry.register(
            ToolDefinition(
                name=AUTHORIZATION_TOOL_NAME,
                description=(
                    "Ask lead to authorize a blocked tool for this teammate. "
                    "This waits until lead explicitly approves or rejects the request."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string"},
                        "reason": {"type": "string"},
                        "argument_summary": {"type": "string"},
                    },
                    "required": ["tool_name", "reason"],
                },
                handler=worker_request_authorization,
            )
        )

    def _submit_plan(self, actor: str, plan: str) -> str:
        request = self.request_tracker.create_plan_request(actor, plan)
        getter = getattr(getattr(self, "team_manager", None), "_member_session_id", None)
        session_id = getter(actor) if callable(getter) else None
        self.bus.send(actor, "lead", plan, "plan_request", {"request_id": request["request_id"]}, session_id=session_id)
        return f"Submitted plan request {request['request_id']}"

    def _request_worker_authorization(
        self,
        actor: str,
        tool_name: str,
        reason: str,
        argument_summary: str = "",
        should_interrupt=None,
    ) -> str:
        normalized_tool = str(tool_name or "").strip()
        normalized_actor = str(actor or "teammate").strip() or "teammate"
        if not normalized_tool:
            return "Authorization request failed: tool_name is required."
        if normalized_tool == AUTHORIZATION_TOOL_NAME:
            return "Authorization not required for request_authorization."
        cached_payload = self._permission_manager().cached_authorization_payload(normalized_tool, include_mode=False)
        if cached_payload is not None:
            return json.dumps(cached_payload, ensure_ascii=False)

        session_id = None
        getter = getattr(getattr(self, "team_manager", None), "_member_session_id", None)
        if callable(getter):
            session_id = getter(normalized_actor)
        summary = str(argument_summary or "").strip()
        request = self.request_tracker.create_authorization_request(
            normalized_actor,
            normalized_tool,
            str(reason or "").strip(),
            summary,
        )
        message = (
            f"{normalized_actor} requests authorization for tool '{normalized_tool}'.\n"
            f"Request: {request['request_id']}\n"
            f"Reason: {str(reason or '').strip() or '(no reason provided)'}"
        )
        if summary:
            message += f"\nArguments: {summary}"
        self.bus.send(
            normalized_actor,
            "lead",
            message,
            "authorization_request",
            {
                "tool_name": normalized_tool,
                "reason": str(reason or "").strip(),
                "argument_summary": summary,
                "request_id": request["request_id"],
            },
            session_id=session_id,
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if callable(should_interrupt) and should_interrupt():
                raise TurnInterrupted("Interrupted while waiting for lead authorization.")
            current = self.request_tracker.get_authorization_request(request["request_id"])
            status = str((current or {}).get("status") or "pending").strip().lower()
            if status != "pending":
                payload = {
                    "status": "approved" if status == "approved" else "rejected",
                    "request_id": request["request_id"],
                    "tool_name": normalized_tool,
                    "scope": str((current or {}).get("scope") or ("once" if status == "approved" else "deny")),
                    "feedback": str((current or {}).get("feedback") or ""),
                }
                return json.dumps(payload, ensure_ascii=False)
            time.sleep(0.2)
        return json.dumps(
            {
                "status": "denied",
                "request_id": request["request_id"],
                "tool_name": normalized_tool,
                "reason": "Timed out waiting for lead authorization.",
            },
            ensure_ascii=False,
        )

    def _environment_guidance(self) -> str:
        return self._system_prompt_builder().environment_guidance()

    def build_system_prompt(
        self,
        actor: str = "lead",
        role: str = "lead coding agent",
        session: AgentSession | None = None,
    ) -> str:
        return self._system_prompt_builder().build_system_prompt(actor=actor, role=role, session=session)

    def build_system_prompt_sections(
        self,
        actor: str = "lead",
        role: str = "lead coding agent",
        session: AgentSession | None = None,
    ) -> list[dict[str, object]]:
        return self._system_prompt_builder().build_system_prompt_sections(actor=actor, role=role, session=session)

    def _base_system_prompt(self) -> str:
        return self._system_prompt_builder().base_system_prompt()

    def create_session(self) -> AgentSession:
        self._current_working_file = None
        session = self.session_manager.create()
        self._hook_manager().on_session_start(session)
        return session

    def inherit_task_board(self, fresh: AgentSession, old: AgentSession) -> None:
        """Bind ``fresh`` to ``old``'s task board (every /new swap inherits).

        The board keeps the id of the session that founded it; the first swap
        lazily migrates that session's task files into the boards/ layout.
        """
        old_board = getattr(old, "task_session_id", None) or self.task_store.ensure_board(old.id)
        fresh.task_session_id = old_board
        self.session_manager.save(fresh)

    def latest_session(self) -> AgentSession:
        self._current_working_file = None
        return self.session_manager.latest_or_create()

    def load_session(self, session_id: str) -> AgentSession:
        self._current_working_file = None
        return self.session_manager.load(session_id)

    def list_sessions(self) -> list[AgentSession]:
        return self.session_manager.list_all()

    def list_session_summaries(self) -> list[dict[str, Any]]:
        return self.session_manager.list_summaries()

    def delete_session(self, session_id: str) -> bool:
        return self.session_manager.delete(session_id)

    def parse_symbol_output(self, output: object) -> list[dict[str, Any]]:
        if not isinstance(output, str):
            return []
        matches: list[dict[str, Any]] = []
        for line in output.splitlines():
            parsed = re.match(r"^(.*?):(\d+):([A-Za-z_]+) (.+)$", line.strip())
            if parsed is None:
                continue
            matches.append(
                {
                    "path": parsed.group(1),
                    "line": int(parsed.group(2)),
                    "kind": parsed.group(3),
                    "name": parsed.group(4),
                }
            )
        return matches

    def render_symbol_preview(self, relative_path: str, line_number: int, *, context_lines: int = 6) -> str:
        path = safe_path(self.settings.workspace_root, relative_path)
        lines = _read_text_with_fallback(path).splitlines()
        if not lines:
            return f"{relative_path}:1\n(empty file)"
        center = max(1, line_number)
        start = max(1, center - context_lines)
        end = min(len(lines), center + context_lines)
        rendered = [f"{relative_path}:{center}"]
        for current in range(start, end + 1):
            marker = ">" if current == center else " "
            rendered.append(f"{marker} {current:4d} | {lines[current - 1]}")
        return "\n".join(rendered)

    def invoke_tool(self, session: AgentSession, name: str, payload: dict[str, Any], *, actor: str = "lead") -> Any:
        ctx = ToolExecutionContext(
            runtime=self,
            session=session,
            actor=actor,
            trace_id=f"{session.id}-interactive-{uuid.uuid4().hex[:8]}",
        )
        return self.registry.execute(ctx, name, payload)

    def _prepare_system_prompt_for_provider(self, system_prompt: Any, provider: LLMProvider | None) -> Any:
        provider_settings = getattr(provider, "settings", None)
        provider_type = str(getattr(provider_settings, "provider_type", "") or "").strip().lower()
        if provider_type == "anthropic":
            return cache_optimized_system_prompt(system_prompt)
        return system_prompt

    def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        text_callback=None,
        thinking_callback=None,
        should_interrupt=None,
    ):
        last_error: Exception | None = None
        attempts = 0
        provider = self._provider_for_messages(messages) or self.provider
        provider_settings = getattr(provider, "settings", self.settings.provider)
        provider_system_prompt = self._prepare_system_prompt_for_provider(system_prompt, provider)
        provider_complete = getattr(provider, "complete")
        # Transient one-shot max_tokens override set by the agent loop when a
        # reasoning model exhausted the budget on thinking. Consumed here so the
        # next completion uses the larger budget exactly once.
        effective_max_tokens = self._consume_transient_max_tokens_override(provider_settings.max_tokens)
        try:
            provider_parameters = inspect.signature(provider_complete).parameters
        except (TypeError, ValueError):
            provider_parameters = {}
        provider_accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in provider_parameters.values()
        )
        include_thinking_callback = "thinking_callback" in provider_parameters or provider_accepts_kwargs
        for attempt in range(1, 4):
            attempts = attempt
            self._raise_if_interrupted(should_interrupt)
            try:
                if should_interrupt is None:
                    kwargs = {
                        "system_prompt": provider_system_prompt,
                        "messages": messages,
                        "tools": tools,
                        "max_tokens": effective_max_tokens,
                        "text_callback": text_callback,
                        "stop_checker": None,
                    }
                    if include_thinking_callback:
                        kwargs["thinking_callback"] = thinking_callback
                    return provider_complete(**kwargs)
                return self._complete_with_interrupt_polling(
                    system_prompt=provider_system_prompt,
                    messages=messages,
                    tools=tools,
                    provider=provider,
                    max_tokens=effective_max_tokens,
                    text_callback=text_callback,
                    thinking_callback=thinking_callback,
                    should_interrupt=should_interrupt,
                )
            except TurnInterrupted:
                raise
            except ProviderError as exc:
                last_error = exc
                if not getattr(exc, "retryable", True):
                    break
                if attempt < 3:
                    self._wait_before_provider_retry(should_interrupt)
            except Exception as exc:
                last_error = exc
                break
        if last_error is None:
            raise RuntimeError("Provider call failed.")
        if attempts <= 1:
            message = f"Provider call failed: {last_error}"
        else:
            message = f"Provider call failed after {attempts} attempts: {last_error}"
        if isinstance(last_error, ProviderError):
            raise ProviderError(
                message,
                retryable=getattr(last_error, "retryable", True),
                kind=getattr(last_error, "kind", "other"),
            ) from last_error
        raise RuntimeError(message)

    def _wait_before_provider_retry(self, should_interrupt=None) -> None:
        delay_seconds = max(0.0, float(getattr(self, "PROVIDER_RETRY_DELAY_SECONDS", 0.0) or 0.0))
        if delay_seconds <= 0:
            return
        deadline = time.monotonic() + delay_seconds
        while True:
            self._raise_if_interrupted(should_interrupt)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(self.PROVIDER_POLL_INTERVAL_SECONDS, remaining))

    def _complete_with_interrupt_polling(
        self,
        *,
        system_prompt: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        provider: LLMProvider | None = None,
        max_tokens: int | None = None,
        text_callback=None,
        thinking_callback=None,
        should_interrupt=None,
    ):
        provider = provider or self.provider
        if max_tokens is None:
            max_tokens = self.settings.provider.max_tokens
        cancel_event = Event()
        result_queue: Queue[tuple[str, Any]] = Queue(maxsize=1)

        def provider_stop_checker() -> bool:
            if cancel_event.is_set():
                return True
            if should_interrupt is not None and should_interrupt():
                cancel_event.set()
                return True
            return False

        def interruptible_callback(text: str) -> None:
            if provider_stop_checker():
                raise TurnInterrupted("Interrupted by user.")
            if text_callback is not None:
                text_callback(text)
            if provider_stop_checker():
                raise TurnInterrupted("Interrupted by user.")

        def run_provider() -> None:
            try:
                provider_complete = getattr(provider, "complete")
                try:
                    provider_parameters = inspect.signature(provider_complete).parameters
                except (TypeError, ValueError):
                    provider_parameters = {}
                provider_accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in provider_parameters.values()
                )
                kwargs = {
                    "system_prompt": system_prompt,
                    "messages": messages,
                    "tools": tools,
                    "max_tokens": max_tokens,
                    "text_callback": interruptible_callback if (text_callback is not None or should_interrupt is not None) else text_callback,
                    "stop_checker": provider_stop_checker,
                }
                if "thinking_callback" in provider_parameters or provider_accepts_kwargs:
                    kwargs["thinking_callback"] = thinking_callback
                turn = provider_complete(**kwargs)
                result_queue.put(("result", turn))
            except BaseException as exc:  # pragma: no cover - exercised via caller assertions
                result_queue.put(("error", exc))

        worker = Thread(target=run_provider, name="open-somnia-provider-call", daemon=True)
        worker.start()

        while True:
            try:
                kind, value = result_queue.get(timeout=self.PROVIDER_POLL_INTERVAL_SECONDS)
            except Empty:
                if provider_stop_checker():
                    raise TurnInterrupted("Interrupted by user.")
                continue
            if kind == "error":
                raise value
            return value

    def run_subagent(
        self,
        prompt: str,
        agent_type: str = "Explore",
        *,
        activity_id: str | None = None,
        should_interrupt=None,
        resume_from=None,
        session_id: str | None = None,
        extra_prompt: str | None = None,
    ):
        return self._subagent_runner().run_subagent(
            prompt,
            agent_type,
            activity_id=activity_id,
            should_interrupt=should_interrupt,
            resume_from=resume_from,
            session_id=session_id,
            extra_prompt=extra_prompt,
        )

    def interrupt_active_teammates(self, reason: str = "lead_interrupt") -> int:
        manager = getattr(self, "team_manager", None)
        interrupter = getattr(manager, "interrupt_active", None)
        if not callable(interrupter):
            return 0
        try:
            return int(interrupter(reason=reason))
        except Exception:
            return 0

    def compact_session(self, session: AgentSession) -> None:
        session.messages = self.compact_manager.auto_compact(session.id, session.messages)
        self._record_session_token_usage(session, getattr(self.compact_manager, "last_usage", None))
        self._note_context_governance(session.id, "manual_compact", "auto-compacted session history")
        try:
            self.context_window_usage(session)
        except Exception:
            pass
        self.session_manager.save(session)

    def checkpoint_session(self, session: AgentSession, tag: str) -> dict[str, Any]:
        """Create a named checkpoint of the session for later rollback.

        Args:
            session: The session to checkpoint.
            tag: A human-readable tag. If empty, auto-generates one.

        Returns:
            Checkpoint metadata dict.
        """
        if not tag.strip():
            existing = self.session_manager.list_checkpoints(session)
            index = len(existing) + 1
            tag = f"checkpoint_{index}"
        return self.session_manager.create_checkpoint(session, tag)

    def rollback_session(self, session: AgentSession, tag: str, *, skip_externally_modified: bool = False) -> dict[str, Any]:
        """Roll back a session to a previously created checkpoint.

        Reverts file changes, truncates messages, restores session state.

        Args:
            session: The session to roll back.
            tag: The checkpoint tag to roll back to.
            skip_externally_modified: If True, skip reverting files that were
                modified externally after the agent's last write.

        Returns:
            Rollback result dict with statistics.
        """
        return self.session_manager.rollback_to_checkpoint(
            session,
            tag,
            workspace_root=self.settings.workspace_root,
            skip_externally_modified=skip_externally_modified,
        )

    def detect_external_modifications(self, session: AgentSession, tag: str) -> list[dict[str, str]]:
        """Detect files modified externally since a checkpoint."""
        return self.session_manager.detect_external_modifications(
            session, tag, self.settings.workspace_root,
        )

    def list_checkpoints(self, session: AgentSession) -> list[dict[str, Any]]:
        """List all checkpoints for a session."""
        return self.session_manager.list_checkpoints(session)

    def _is_visible_conversation_message(self, message: dict[str, Any]) -> bool:
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            return True
        if role != "user" or not isinstance(content, str):
            return False
        return not (content.startswith("<background-results>") or content.startswith("<inbox>"))

    def _lead_inbox_has_messages(self) -> bool:
        checker = getattr(getattr(self, "bus", None), "has_inbox_messages", None)
        if not callable(checker):
            return False
        try:
            return bool(checker("lead"))
        except Exception:
            return False

    def _is_internal_lead_inbox_message(self, message: dict[str, Any]) -> bool:
        message_type = str(message.get("type", "")).strip()
        if message_type != "shutdown_response":
            return False
        request_id = str(message.get("request_id", "")).strip()
        if request_id:
            marker = getattr(getattr(self, "request_tracker", None), "mark_shutdown_response", None)
            if callable(marker):
                try:
                    marker(request_id, "accepted")
                except Exception:
                    pass
        return True

    def _drain_lead_visible_inbox(self, session: AgentSession) -> list[dict[str, Any]]:
        try:
            inbox = self.bus.read_inbox("lead", session_id=session.id)
        except TypeError:
            inbox = self.bus.read_inbox("lead")
        return [message for message in inbox if not self._is_internal_lead_inbox_message(message)]

    def _active_task_preserve_index(
        self,
        messages: list[dict[str, Any]],
        task_anchor_message: dict[str, Any] | None,
    ) -> int | None:
        if task_anchor_message is None:
            return None
        anchor_index = None
        for index, message in enumerate(messages):
            if message is task_anchor_message:
                anchor_index = index
                break
        if anchor_index is None:
            return None

        preserve_index = anchor_index
        previous_visible_index = None
        for index in range(anchor_index - 1, -1, -1):
            if self._is_visible_conversation_message(messages[index]):
                previous_visible_index = index
                break
        if previous_visible_index is None:
            return preserve_index
        preserve_index = previous_visible_index

        if messages[previous_visible_index].get("role") == "assistant":
            for index in range(previous_visible_index - 1, -1, -1):
                if not self._is_visible_conversation_message(messages[index]):
                    continue
                if messages[index].get("role") == "user":
                    preserve_index = index
                break
        return preserve_index

    def _raise_if_interrupted(self, should_interrupt) -> None:
        if should_interrupt is not None and should_interrupt():
            raise TurnInterrupted("Interrupted by user.")

    def _count_open_todo_items(self, session: AgentSession | None) -> int:
        if session is None:
            return 0
        count = 0
        for item in list(getattr(session, "todo_items", []) or []):
            status = str(item.get("status", "pending")).strip().lower()
            if status in {"pending", "in_progress"}:
                count += 1
        return count

    def _agent_loop_result(self, text: str, *, status: str, session: AgentSession | None) -> AgentLoopResult:
        return AgentLoopResult(
            text,
            status=status,
            open_todo_count=self._count_open_todo_items(session),
        )

    def _max_tool_calls_per_turn(self) -> int:
        runtime_settings = getattr(self.settings, "runtime", None)
        raw_limit = getattr(runtime_settings, "max_tool_calls_per_turn", self.DEFAULT_MAX_TOOL_CALLS_PER_TURN)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return self.DEFAULT_MAX_TOOL_CALLS_PER_TURN
        return max(1, limit)

    def _runtime_non_negative_int(self, name: str, default: int) -> int:
        runtime_settings = getattr(self.settings, "runtime", None)
        raw_value = getattr(runtime_settings, name, default)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return default
        return max(0, value)

    def _exploration_soft_limit(self) -> int:
        return self._runtime_non_negative_int("exploration_soft_limit", self.EXPLORATION_SOFT_LIMIT)

    def _exploration_hard_streak_limit(self) -> int:
        return self._runtime_non_negative_int("exploration_hard_streak_limit", self.EXPLORATION_HARD_STREAK_LIMIT)

    def _exploration_hard_total_limit(self) -> int:
        return self._runtime_non_negative_int("exploration_hard_total_limit", self.EXPLORATION_HARD_TOTAL_LIMIT)

    def _empty_response_max_streak(self) -> int:
        return self._runtime_non_negative_int("empty_response_max_streak", self.EMPTY_RESPONSE_MAX_STREAK)

    def _exploration_budget_error(
        self,
        tool_name: str,
        *,
        next_streak: int,
        next_total: int,
        hard_streak_limit: int,
        hard_total_limit: int,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if hard_streak_limit > 0 and next_streak > hard_streak_limit:
            reasons.append(f"{hard_streak_limit} consecutive read/search calls")
        if hard_total_limit > 0 and next_total > hard_total_limit:
            reasons.append(f"{hard_total_limit} total read/search calls this turn")
        limit_text = " and ".join(reasons) or "the configured exploration budget"
        return make_tool_error(
            tool_name,
            "exploration_budget_exceeded",
            (
                f"Exploration budget exceeded after {limit_text}. "
                "Stop exploring in the main context now and delegate the remaining inspection to a `subagent` "
                "(it runs in its own clean context and returns a summary), or provide a concise interim "
                "conclusion from the evidence already gathered. "
                "If more inspection is genuinely necessary, ask the user to continue with the smallest specific next check."
            ),
        )

    def _known_tool_names(self) -> set[str]:
        names = getattr(self.registry, "names", None)
        if not callable(names):
            return set()
        try:
            return {str(name) for name in names()}
        except Exception:
            return set()

    # ----- Lead tool-call planning (order-preserving parallel dispatch) -----
    #
    # The lead loop used to execute tool calls strictly serially and interleave
    # guard decisions (flood guard, malformed/unknown name, exploration budget)
    # with execution and side effects. To run independent read-only tools
    # concurrently without changing observable behavior, the loop is split into
    # three stages:
    #
    #   Stage A (``_plan_lead_tool_calls``): a pure, deterministic pre-scan that
    #       reproduces the exact guard/counter sequence the serial loop would
    #       have produced, yielding a ``PlannedCall`` per tool call. Every guard
    #       decision here depends only on tool name + the counters carried from
    #       the previous call, never on execution output -- so pre-scanning the
    #       whole turn is equivalent to deciding one-at-a-time.
    #
    #   Stage B/C (the loop body): iterate the plan in order, executing maximal
    #       runs of parallel-safe EXECUTE calls on the shared pool (via
    #       ``dispatch_parallel_segment``) and every other call inline, then
    #       applying side effects (UI, transcript, counters) in input order.
    #       The stateful interrupts (turn-boundary tools,
    #       ``prepare_next_loop_user_message``) are checked at segment
    #       boundaries, not pre-scanned: they are time-sensitive (e.g. loop
    #       injections arriving via the API during execution) and must be
    #       evaluated during the live execution flow.

    def _plan_lead_tool_calls(
        self,
        tool_calls: list[Any],
        *,
        max_tool_calls: int,
        known_tool_names: set[str],
        exploration_streak: int,
        exploration_total: int,
        exploration_soft_limit: int,
        exploration_hard_streak_limit: int,
        exploration_hard_total_limit: int,
    ) -> tuple[list["_PlannedLeadCall"], int, int, bool]:
        """Deterministically reproduce the serial guard/counter sequence.

        Returns ``(plan, final_exploration_streak, final_exploration_total,
        pending_exploration_summary_reminder)``. ``plan`` is in input order;
        duplicate malformed/unknown names are dropped (mirroring the serial
        ``continue``) and never produce a result.
        """
        plan: list[_PlannedLeadCall] = []
        reported_invalid: set[tuple[str, str]] = set()
        reported = 0
        streak = exploration_streak
        total = exploration_total
        pending_summary = False
        end_turn = False
        for tool_call in tool_calls:
            tool_name = str(tool_call.name or "")
            is_exploration = self._is_exploration_tool_call(tool_name, tool_call.input)
            decision: str = "execute"
            output: Any = None
            if reported >= max_tool_calls:
                output = make_tool_error(
                    tool_name,
                    "too_many_tool_calls",
                    (
                        f"Tool call flood guard stopped this turn after {max_tool_calls} reported tool "
                        f"call(s); skipped the remaining {max(0, len(tool_calls) - reported)}."
                    ),
                )
                decision = "flood_error"
                end_turn = True
            else:
                malformed_reason = self._malformed_tool_name_reason(tool_name)
                if malformed_reason is not None:
                    key = ("malformed_tool_name", tool_name)
                    if key in reported_invalid:
                        # Serial ``continue`` -> dropped, no result, no counters.
                        continue
                    reported_invalid.add(key)
                    output = make_tool_error(
                        tool_name,
                        "malformed_tool_name",
                        (
                            f"Malformed tool call name '{tool_name}': {malformed_reason}. "
                            "Use exactly one of the registered tool names and send arguments as JSON."
                        ),
                    )
                    decision = "malformed_error"
                elif known_tool_names and tool_name not in known_tool_names:
                    key = ("unknown_tool", tool_name)
                    if key in reported_invalid:
                        continue
                    reported_invalid.add(key)
                    output = make_tool_error(
                        tool_name,
                        "unknown_tool",
                        (
                            f"Unknown tool: {tool_name}. "
                            f"Available tools: {', '.join(sorted(known_tool_names))}."
                        ),
                    )
                    decision = "unknown_error"
                elif is_exploration:
                    next_streak = streak + 1
                    next_total = total + 1
                    if (
                        (exploration_hard_streak_limit > 0 and next_streak > exploration_hard_streak_limit)
                        or (exploration_hard_total_limit > 0 and next_total > exploration_hard_total_limit)
                    ):
                        output = self._exploration_budget_error(
                            tool_name,
                            next_streak=next_streak,
                            next_total=next_total,
                            hard_streak_limit=exploration_hard_streak_limit,
                            hard_total_limit=exploration_hard_total_limit,
                        )
                        decision = "budget_error"
                        pending_summary = True
                        end_turn = True
                    # else decision stays "execute"; streak/total updated below.
            # A planned call always consumes a report slot and a result, except
            # for dropped duplicates (handled by ``continue`` above).
            parallel_safe = decision == "execute" and (
                is_parallel_safe(tool_name) or is_explore_subagent_safe(tool_call)
            )
            plan.append(
                _PlannedLeadCall(
                    tool_call=tool_call,
                    tool_name=tool_name,
                    is_exploration=is_exploration,
                    decision=decision,
                    guard_output=output,
                    parallel_safe=parallel_safe,
                    is_turn_boundary=tool_name in self.TURN_BOUNDARY_TOOL_NAMES,
                    end_turn_after=end_turn,
                )
            )
            reported += 1
            # Counter update mirrors agent.py 3707-3727 exactly.
            if is_exploration and decision != "budget_error":
                streak += 1
                total += 1
                if (
                    (exploration_soft_limit > 0 and streak >= exploration_soft_limit)
                    or (exploration_hard_streak_limit > 0 and streak >= exploration_hard_streak_limit)
                    or (exploration_hard_total_limit > 0 and total >= exploration_hard_total_limit)
                ):
                    pending_summary = True
            elif not is_exploration:
                streak = 0
                pending_summary = False
            # Flood guard and exploration budget set ``end_turn_after_tool`` in
            # the serial loop, which then ``break``s -- so the scan stops here
            # too. The stateful breaks (turn-boundary tools,
            # ``prepare_next_loop_user_message``) are not pre-scannable and are
            # evaluated live during execution instead.
            if end_turn:
                break
        return plan, streak, total, pending_summary

    def _malformed_tool_name_reason(self, tool_name: str) -> str | None:
        normalized = str(tool_name or "").strip()
        if not normalized:
            return "empty tool name"
        if any(marker in normalized for marker in ("<", ">", "\r", "\n", "\t")):
            return "tool name contains markup or control characters"
        return None

    def _normalize_user_input_message(self, user_input: Any) -> tuple[dict[str, Any], str]:
        if isinstance(user_input, dict):
            message = {
                "role": str(user_input.get("role", "user")).strip() or "user",
                "content": user_input.get("content", ""),
            }
        else:
            embedded_message = decode_embedded_user_message(user_input)
            if embedded_message is not None:
                message = embedded_message
            else:
                message = make_user_text_message(str(user_input))
        if message["role"] != "user":
            message["role"] = "user"
        latest_user_message = render_text_content(message.get("content", ""))
        return message, latest_user_message

    def _append_transcript_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        transcript_entry = deepcopy(entry)
        if isinstance(transcript_entry, dict):
            consume_ephemeral_image_blocks([transcript_entry])
        self.transcript_store.append(session_id, transcript_entry)

    def _attach_thinking_log_marker(
        self,
        assistant_message: dict[str, Any],
        *,
        thinking_log: ThinkingLogWriter | None,
        thinking_callback=None,
        notify_finished: bool = True,
    ) -> dict[str, Any]:
        thinking_blocks = extract_thinking_blocks(assistant_message)
        if thinking_blocks and thinking_log is not None and not thinking_log.has_content:
            for block in thinking_blocks:
                thinking_log.append_block(block)
        message = strip_thinking_log_blocks_from_message(assistant_message)
        if thinking_log is None or not thinking_log.has_content:
            return message
        marker = thinking_log.marker()
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [marker, *content]
        elif isinstance(content, str):
            blocks: list[dict[str, Any]] = [marker]
            if content:
                blocks.append({"type": "text", "text": content})
            message["content"] = blocks
        else:
            message["content"] = [marker]
        if notify_finished and callable(thinking_callback):
            thinking_callback({"event": "finished", **marker})
        return message

    def _append_subagent_placeholders(self, session, seg_plans, *, turn=None):
        """Pre-write the subagent tool_call + a placeholder tool_result into
        ``session.messages`` (and the transcript) BEFORE the subagents run.

        Why: when a subagent is interrupted, the assistant turn carrying its
        tool_call is otherwise never appended (the round-end append at the
        bottom of the loop is skipped on interrupt), so the lead has no record
        that the subagent ever ran -- and after "continue" it cannot decide to
        resume. By writing the tool_call + a ``running`` placeholder up front we
        guarantee the lead sees the subagent on the next turn no matter what.

        ``turn`` is the provider turn the tool calls came from. Its thinking
        blocks must ride along: thinking-enabled providers reject the next
        request when an assistant message carries tool_use without the thinking
        blocks that preceded it ("content[].thinking ... must be passed back"),
        and this placeholder message is the ONLY persisted record of the turn's
        tool calls (the round-end append skips placeholder ids). Text blocks
        stay out -- mixed rounds persist them via the round-end append, and
        placeholder-only rounds carry no user-facing text.

        Returns ``(assistant_message, placeholder_items)``: the appended
        assistant message (so the caller can drop it from the round-end append
        to avoid a duplicate) and the list of placeholder tool_result items
        (held by reference so the caller rewrites them in place on completion
        or interrupt).
        """
        blocks: list[dict[str, Any]] = []
        for block in getattr(turn, "content_blocks", None) or []:
            if is_thinking_block(block):
                blocks.append(dict(block))
        for p in seg_plans:
            tool_call = p.tool_call
            block = {
                "type": "tool_call",
                "id": tool_call.id,
                "name": tool_call.name,
                "input": tool_call.input,
            }
            if getattr(tool_call, "importance", None):
                block["importance"] = tool_call.importance
            blocks.append(block)
        assistant_message = {"role": "assistant", "content": blocks}
        session.messages.append(assistant_message)
        self._append_transcript_entry(session.id, assistant_message)

        placeholder_items: list[dict[str, Any]] = []
        for p in seg_plans:
            aid = p.tool_call.id
            placeholder_output = {
                "status": "running",
                "message": (
                    f"subagent 执行中。若被中断，用 resume_from=\"{aid}\" 恢复"
                    f"（继承已累积的上下文，低成本），可附 extra_prompt 补充新要求。"
                ),
                "activity_id": aid,
            }
            item = make_tool_result_item(
                aid,
                placeholder_output,
                rendered_output=serialize_tool_output(placeholder_output),
            )
            placeholder_items.append(item)
        tool_result_message = make_tool_result_message(list(placeholder_items))
        session.messages.append(tool_result_message)
        self._append_transcript_entry(session.id, tool_result_message)
        return assistant_message, placeholder_items

    def _finalize_placeholders_completed(self, placeholder_items, seg_records) -> None:
        """Rewrite the running placeholder tool_result items in place with the
        real subagent results. The items are already in ``session.messages`` (by
        reference), so mutating them here is what the lead will see."""
        for item, record in zip(placeholder_items, seg_records):
            persisted = record.persisted_output
            rendered = record.rendered_output
            item["content"] = rendered
            item.pop("is_error", None)
            # Mirror make_tool_result_item's tool_result_text channel so the
            # lead sees the clean summary text for completed subagents.
            if isinstance(persisted, dict):
                tr_text = persisted.get("tool_result_text")
                if isinstance(tr_text, str) and tr_text.strip():
                    item["content"] = tr_text.strip()
                    item["tool_result_text"] = tr_text.strip()
                    item.pop("is_error", None)
                else:
                    status = str(persisted.get("status", "")).strip().lower()
                    if bool(persisted.get("is_error")) or status in {"error", "failed", "denied", "interrupted", "truncated"}:
                        item["is_error"] = True
            item["raw_output"] = persisted

    def _finalize_placeholders_interrupted(self, placeholder_items, seg_plans) -> None:
        """Rewrite the running placeholder tool_result items in place to mark
        the subagent as interrupted, with the resume pointer. Called before
        re-raising TurnInterrupted so the session save in the interrupt handler
        captures the rewritten state."""
        for item, p in zip(placeholder_items, seg_plans):
            aid = p.tool_call.id
            payload = {
                "status": "interrupted",
                "error_type": "subagent_interrupted",
                "tool_name": "subagent",
                "message": (
                    f"subagent 被用户中断（上下文已保留）。若用户要继续，用 "
                    f"resume_from=\"{aid}\" 恢复（可附 extra_prompt 补充新要求）；"
                    f"若用户改方向，忽略它继续。不要从头重新派 subagent。"
                ),
                "activity_id": aid,
                "is_error": True,
            }
            item["content"] = serialize_tool_output(payload)
            item["is_error"] = True

    def run_turn(
        self,
        session: AgentSession,
        user_input: str | dict[str, Any],
        text_callback=None,
        thinking_callback=None,
        should_interrupt=None,
        take_next_loop_user_message=None,
        prepare_next_loop_user_message=None,
    ) -> AgentLoopResult:
        session.pending_file_changes = []
        session.last_turn_file_changes = []
        activator = getattr(getattr(self, "team_manager", None), "activate_session", None)
        if callable(activator):
            activator(session.id)
        task_anchor_message, _latest_user_message = self._normalize_user_input_message(user_input)
        session.messages.append(task_anchor_message)
        self._append_transcript_entry(session.id, task_anchor_message)
        return self._agent_loop(
            session,
            text_callback=text_callback,
            thinking_callback=thinking_callback,
            should_interrupt=should_interrupt,
            task_anchor_message=task_anchor_message,
            take_next_loop_user_message=take_next_loop_user_message,
            prepare_next_loop_user_message=prepare_next_loop_user_message,
        )

    def _agent_loop(
        self,
        session: AgentSession,
        text_callback=None,
        thinking_callback=None,
        should_interrupt=None,
        task_anchor_message=None,
        take_next_loop_user_message=None,
        prepare_next_loop_user_message=None,
    ) -> AgentLoopResult:
        final_text = ""
        pending_tool_repair_hints: list[dict[str, Any]] = []
        pending_todo_reconcile = False
        exploration_streak = 0
        exploration_total = 0
        pending_exploration_summary_reminder = False
        # Counter of consecutive thinking-only turns (no text, no tool calls).
        # Replaces the old boolean flag: the first nudges retry, repeated empty
        # responses trip a circuit breaker instead of looping to max_agent_rounds.
        pending_empty_response_repair = False
        empty_response_streak = 0
        pending_reasoning_budget_notice = False
        try:
            exploration_soft_limit = self._exploration_soft_limit()
            exploration_hard_streak_limit = self._exploration_hard_streak_limit()
            exploration_hard_total_limit = self._exploration_hard_total_limit()
            empty_response_max_streak = self._empty_response_max_streak()
            for _ in range(self.settings.runtime.max_agent_rounds):
                self._raise_if_interrupted(should_interrupt)
                loop_user_message = None
                if callable(take_next_loop_user_message):
                    loop_user_message = take_next_loop_user_message()
                if loop_user_message:
                    task_anchor_message, _latest_user_message = self._normalize_user_input_message(loop_user_message)
                    session.messages.append(task_anchor_message)
                    self._append_transcript_entry(session.id, task_anchor_message)
                background_notifications = self.background_manager.drain()
                if background_notifications:
                    text = "\n".join(
                        f"[bg:{item['task_id']}] {item['status']}: {item['result']}" for item in background_notifications
                    )
                    session.messages.append(make_user_text_message(f"<background-results>\n{text}\n</background-results>"))
                inbox = self._drain_lead_visible_inbox(session)
                if inbox:
                    session.messages.append(make_user_text_message(f"<inbox>{json.dumps(inbox, ensure_ascii=False, indent=2)}</inbox>"))
                if should_auto_compact(self.context_window_usage(session)):
                    preserve_from_index = self._active_task_preserve_index(session.messages, task_anchor_message)
                    session.messages = self.compact_manager.auto_compact(
                        session.id,
                        session.messages,
                        preserve_from_index=preserve_from_index,
                    )
                    self._record_session_token_usage(session, getattr(self.compact_manager, "last_usage", None))
                    self._note_context_governance(session.id, "auto_compact", "auto-compacted older history")
                    try:
                        self.context_window_usage(session)
                    except Exception:
                        pass
                # Runs after the compact check: post-compact usage decides
                # whether a pressure episode is opening, escalating, or over.
                self._maybe_append_context_pressure_message(session)

                stream_flush_callback = getattr(text_callback, "finish", None) if text_callback is not None else None
                streamed_text_chunks: list[str] = []
                completion_text_callback = text_callback

                try:
                    system_prompt = self.build_system_prompt(session=session)
                except TypeError:
                    system_prompt = self.build_system_prompt()
                tool_schemas = self._tool_schemas_for_model("lead")
                transient_notices: list[str] = []
                if session.rounds_without_todo >= 3 and self.todo_manager.has_open_items(session):
                    transient_notices.append(self.TODO_STALE_STATUS_REMINDER_TEXT)
                if pending_todo_reconcile:
                    transient_notices.append(self.TODO_RECONCILE_REMINDER_TEXT)
                if pending_empty_response_repair:
                    # Escalate the nudge wording as the streak grows so the model
                    # is pushed harder toward acting instead of re-planning.
                    if empty_response_streak >= 2:
                        transient_notices.append(
                            "Reminder: again, no visible text or tool calls. Stop planning now and call a "
                            "tool or write the answer directly. Keep any reasoning minimal."
                        )
                    else:
                        transient_notices.append(self.EMPTY_ASSISTANT_RESPONSE_REPAIR_TEXT)
                    pending_empty_response_repair = False
                if pending_reasoning_budget_notice:
                    transient_notices.append(self.EMPTY_RESPONSE_REASONING_BUMP_NOTICE)
                    pending_reasoning_budget_notice = False
                if pending_exploration_summary_reminder:
                    transient_notices.append(
                        self._exploration_summary_reminder(streak=exploration_streak, total=exploration_total)
                    )
                    pending_exploration_summary_reminder = False
                if pending_tool_repair_hints:
                    repair_message = render_transient_repair_hint_message(pending_tool_repair_hints)
                    pending_tool_repair_hints = []
                    if repair_message:
                        transient_notices.append(repair_message)
                transient_payload_messages: list[dict[str, Any]] = []
                transient_notice_message = self._runtime_notice_message(transient_notices)
                if transient_notice_message is not None:
                    transient_payload_messages.append(transient_notice_message)
                payload_source_messages = session.messages
                payload_session: AgentSession | None = session
                if transient_payload_messages:
                    payload_source_messages = [*session.messages, *transient_payload_messages]
                    payload_session = None
                    payload_messages = build_payload_messages(
                        payload_source_messages,
                        preserve_thinking_blocks=self._preserve_provider_thinking_blocks(),
                    )
                else:
                    payload_messages = self._messages_for_model(
                        payload_source_messages,
                        session=payload_session,
                        system_prompt=system_prompt,
                        tools=tool_schemas,
                    )
                request_provider = self._provider_for_messages(payload_messages)
                self._consume_ephemeral_image_history(session.messages, session_id=session.id)
                # Reflect the effective max_tokens (incl. a staged reasoning-budget
                # bump) in the diagnostic dump; complete() consumes the override.
                effective_max_tokens_for_dump = getattr(self, "_transient_max_tokens_override", None)
                if not isinstance(effective_max_tokens_for_dump, int):
                    effective_max_tokens_for_dump = self.settings.provider.max_tokens
                dump_path = self._dump_provider_payload_if_enabled(
                    session=session,
                    system_prompt=system_prompt,
                    payload_messages=payload_messages,
                    tools=tool_schemas,
                    max_tokens=effective_max_tokens_for_dump,
                    provider=request_provider,
                    actor="lead",
                    stream=text_callback is not None or should_interrupt is not None,
                    kind="turn",
                )
                session.latest_turn_id = uuid.uuid4().hex[:8]
                provider_turn_id = str(session.latest_turn_id)
                transcript_root = getattr(getattr(self, "transcript_store", None), "root", None)
                thinking_log = (
                    ThinkingLogWriter(Path(transcript_root), session.id, provider_turn_id)
                    if transcript_root is not None
                    else None
                )
                thinking_finished_notified = False

                def notify_thinking_finished_if_needed() -> bool:
                    nonlocal thinking_finished_notified
                    if thinking_finished_notified:
                        return False
                    if thinking_log is None or not thinking_log.has_content:
                        return False
                    if callable(thinking_callback):
                        thinking_callback({"event": "finished", **thinking_log.marker()})
                        thinking_finished_notified = True
                        return True
                    return False

                def record_thinking(block: dict[str, Any]) -> None:
                    event_type = str(block.get("event", "") or "").strip()
                    if event_type == "delta":
                        delta = str(block.get("delta", "") or "")
                        if thinking_log is not None:
                            thinking_log.append_delta(delta)
                        if callable(thinking_callback):
                            thinking_callback(
                                {
                                    "event": "delta",
                                    "session_id": session.id,
                                    "turn_id": provider_turn_id,
                                    "delta": delta,
                                    "block": dict(block),
                                    "path": str(thinking_log.path) if thinking_log is not None else "",
                                    "characters": thinking_log.characters if thinking_log is not None else len(delta),
                                    "block_count": thinking_log.block_count if thinking_log is not None else 0,
                                }
                            )
                        return
                    if thinking_log is not None:
                        thinking_log.append_block(block)
                    delta = str(block.get("thinking", "") or block.get("data", "") or "")
                    if callable(thinking_callback):
                        thinking_callback(
                            {
                                "event": "delta",
                                "session_id": session.id,
                                "turn_id": provider_turn_id,
                                "delta": delta,
                                "block": dict(block),
                                "path": str(thinking_log.path) if thinking_log is not None else "",
                                "characters": thinking_log.characters if thinking_log is not None else len(delta),
                                "block_count": thinking_log.block_count if thinking_log is not None else 0,
                            }
                        )

                started_at = time.monotonic()
                if text_callback is not None:
                    def completion_text_callback(text: str) -> None:
                        text_value = str(text)
                        streamed_text_chunks.append(text_value)
                        if text_value.strip():
                            notify_thinking_finished_if_needed()
                        text_callback(text)

                try:
                    complete = getattr(self, "complete")
                    try:
                        complete_parameters = inspect.signature(complete).parameters
                    except (TypeError, ValueError):
                        complete_parameters = {}
                    complete_accepts_kwargs = any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in complete_parameters.values()
                    )
                    complete_kwargs = {
                        "text_callback": completion_text_callback,
                        "should_interrupt": should_interrupt,
                    }
                    if "thinking_callback" in complete_parameters or complete_accepts_kwargs:
                        complete_kwargs["thinking_callback"] = record_thinking
                    turn = complete(
                        system_prompt,
                        payload_messages,
                        tool_schemas,
                        **complete_kwargs,
                    )
                except Exception as exc:
                    self._record_provider_payload_result(
                        dump_path,
                        error=exc,
                        latency_ms=(time.monotonic() - started_at) * 1000,
                    )
                    raise
                self._record_provider_payload_result(
                    dump_path,
                    turn=turn,
                    latency_ms=(time.monotonic() - started_at) * 1000,
                )
                self._record_session_token_usage(
                    session,
                    self._normalize_turn_usage(
                        turn,
                        system_prompt=system_prompt,
                        messages=payload_messages,
                        tools=tool_schemas,
                    ),
                )
                self._raise_if_interrupted(should_interrupt)
                turn_text = "\n\n".join(turn.text_blocks).strip()
                if turn.has_tool_calls() and text_callback is not None and not "".join(streamed_text_chunks).strip():
                    if turn_text:
                        notify_thinking_finished_if_needed()
                        text_callback(turn_text)
                if callable(stream_flush_callback):
                    stream_flush_callback()
                if not turn.has_tool_calls():
                    assistant_message = turn.as_message()
                    assistant_message = self._attach_thinking_log_marker(
                        assistant_message,
                        thinking_log=thinking_log,
                        thinking_callback=thinking_callback,
                        notify_finished=not thinking_finished_notified,
                    )
                    session.messages.append(assistant_message)
                    self._append_transcript_entry(session.id, assistant_message)
                    final_text = turn_text
                    if final_text:
                        exploration_streak = 0
                        pending_exploration_summary_reminder = False
                        empty_response_streak = 0
                    self._capture_turn_file_changes(session)
                    self.session_manager.save(session)
                    if not final_text:
                        # Thinking-only turn: no visible text and no tool calls.
                        # The model may have burned the whole max_tokens budget on
                        # reasoning and been truncated mid-thought. Try to recover
                        # once by raising the budget (C); if it keeps happening or
                        # no bump is possible, trip the circuit breaker (B) instead
                        # of looping up to max_agent_rounds.
                        empty_response_streak += 1
                        bumped = self._maybe_raise_reasoning_budget(turn)
                        if bumped is not None:
                            # Budget raised for the next completion (one-shot).
                            pending_reasoning_budget_notice = True
                            pending_empty_response_repair = True
                            continue
                        if empty_response_max_streak > 0 and empty_response_streak >= empty_response_max_streak:
                            return AgentLoopResult(
                                self.EMPTY_RESPONSE_STOPPED_TEXT,
                                status="stopped_empty_response",
                                open_todo_count=self._count_open_todo_items(session),
                            )
                        pending_empty_response_repair = True
                        continue
                    self._hook_manager().on_assistant_response(
                        session,
                        actor="lead",
                        trace_id=f"{session.id}-{session.latest_turn_id}",
                        assistant_message=assistant_message,
                        text=final_text,
                        execution_mode=getattr(self, "execution_mode", DEFAULT_EXECUTION_MODE),
                    )
                    if (
                        self.todo_manager.has_open_items(session)
                        and session.rounds_without_todo > 0
                        and not pending_todo_reconcile
                    ):
                        pending_todo_reconcile = True
                        continue
                    if self._drain_lead_visible_inbox(session):
                        continue
                    return self._agent_loop_result(final_text, status="completed", session=session)

                if turn_text:
                    # Treat a visible interim conclusion as the boundary between exploration bursts.
                    exploration_streak = 0
                    pending_exploration_summary_reminder = False
                # A tool-producing turn breaks the thinking-only streak.
                empty_response_streak = 0

                if not thinking_finished_notified:
                    pre_tool_thinking_message = self._attach_thinking_log_marker(
                        turn.as_message([]),
                        thinking_log=thinking_log,
                        thinking_callback=thinking_callback,
                    )
                    thinking_finished_notified = self._assistant_message_has_thinking_log(pre_tool_thinking_message)

                tool_results: list[dict[str, Any]] = []
                executed_tool_calls = []
                used_todo = False
                manual_compact = False
                end_turn_after_tool = False
                max_tool_calls = self._max_tool_calls_per_turn()
                known_tool_names = self._known_tool_names()

                # Stage A: deterministic pre-scan reproducing the serial
                # guard/counter sequence. Counter updates (exploration
                # streak/total) are computed here; the loop below only applies
                # side effects (UI, transcript, stateful interrupts) in order.
                plan, exploration_streak, exploration_total, pending_exploration_summary_reminder = (
                    self._plan_lead_tool_calls(
                        turn.tool_calls,
                        max_tool_calls=max_tool_calls,
                        known_tool_names=known_tool_names,
                        exploration_streak=exploration_streak,
                        exploration_total=exploration_total,
                        exploration_soft_limit=exploration_soft_limit,
                        exploration_hard_streak_limit=exploration_hard_streak_limit,
                        exploration_hard_total_limit=exploration_hard_total_limit,
                    )
                )
                # Stale counters are now authoritative from the plan; the loop
                # body must not recompute them.
                reported_tool_calls = len(plan)

                # Stages B+C: execute and apply side effects in one ordered pass.
                #
                # The original loop interleaved execution with the stateful
                # interrupts (turn-boundary tools, ``prepare_next_loop_user_message``)
                # -- an interrupt could stop the turn *before* later calls ran.
                # So we must not pre-execute the whole plan and then break:
                # execution and interrupt checks are fused here, in input order.
                # Parallelism happens only *inside* a maximal run of parallel-safe
                # read-only calls (which carry no side effects, so an injection
                # arriving mid-run is harmlessly deferred to the run's end -- it
                # only takes effect on the next loop iteration anyway). Everything
                # else (writes, shell, subagent, stateful and turn-boundary tools,
                # guard errors) runs one at a time with the exact same per-call
                # interrupt/break semantics as before.
                trace_id = f"{session.id}-{session.latest_turn_id}"
                parallel_on = parallel_dispatch_enabled(self.settings)

                def _lead_ctx() -> ToolExecutionContext:
                    return ToolExecutionContext(
                        runtime=self,
                        session=session,
                        actor="lead",
                        trace_id=trace_id,
                        should_interrupt=should_interrupt,
                    )

                cursor = 0
                # Subagent tool_calls whose assistant+tool_result pair was
                # already written as a placeholder (and rewritten in place on
                # completion/interrupt). The round-end append must skip these to
                # avoid duplicates. See _append_subagent_placeholders.
                placeholder_assistant_messages: list[dict[str, Any]] = []
                placeholder_tool_call_ids: set[str] = set()
                while cursor < len(plan):
                    self._raise_if_interrupted(should_interrupt)
                    current = plan[cursor]
                    # ``head_kind`` distinguishes the two dispatch pools a
                    # parallel-safe segment can use (``tool`` -> ``_POOL``,
                    # ``subagent`` -> ``_SUBAGENT_POOL``). Defaulted here so the
                    # inline single-call branch never references an unbound name.
                    head_kind = "tool"
                    # Set non-None only by the parallel Explore-subagent branch
                    # to signal that ``print_tool_started`` was pre-fired.
                    subagent_started_ids: set[str] | None = None
                    # Determine the segment: a maximal run of parallel-safe
                    # EXECUTE calls (concurrent), or a single call (inline).
                    # Read-only tools and Explore-subagent calls are *both*
                    # parallel-safe but dispatch on DIFFERENT pools (read-only
                    # tools on ``_POOL`` via ``dispatch_parallel_segment``;
                    # Explore subagents on ``_SUBAGENT_POOL`` via
                    # ``run_parallel_explore_subagents`` -- a nested agent loop
                    # must not consume a ``_POOL`` worker or it deadlocks). So a
                    # maximal run is also bounded by *kind*: it stops at the
                    # first call whose safe-kind differs from the run's head.
                    if parallel_on and current.decision == "execute" and current.parallel_safe:
                        head_kind = self._parallel_safe_kind(current.tool_call)
                        run_end = cursor
                        while (
                            run_end < len(plan)
                            and plan[run_end].decision == "execute"
                            and plan[run_end].parallel_safe
                            and self._parallel_safe_kind(plan[run_end].tool_call) == head_kind
                        ):
                            run_end += 1
                    else:
                        run_end = cursor + 1
                    seg_plans = plan[cursor:run_end]
                    # Tool-call ids for which ``print_tool_started`` was pre-fired
                    # BEFORE execution (normal tools), so the side-effect loop
                    # below skips the redundant post-execute start event. Subagents
                    # track their own pre-fire via ``subagent_started_ids``.
                    pre_fired_tool_ids: set[str] = set()
                    # Execute the segment.
                    if current.decision != "execute":
                        seg_records = [
                            finalize_tool_call(
                                current.tool_call,
                                current.guard_output,
                                max_content_chars=self.settings.runtime.max_tool_output_chars,
                            )
                        ]
                    elif run_end - cursor > 1 and head_kind == "subagent":
                        # Parallel Explore subagents: dispatch on the dedicated
                        # subagent pool (not ``_POOL``) to avoid nested-loop
                        # deadlock. ``run_parallel_explore_subagents`` returns
                        # structured subagent outputs (dicts); wrap each into a
                        # ``ToolCallRecord`` so the downstream side-effect loop
                        # is uniform.
                        #
                        # Pre-fire ``print_tool_started`` for EVERY subagent in
                        # the segment BEFORE dispatching, so the UI registers
                        # one active-subagent slot per parallel subagent (keyed
                        # by ``tool_call.id``) up front. Each subagent's
                        # internal activity (keyed by the same id via
                        # ``_invoke_subagent``) then routes to the right slot.
                        # Without this pre-fire the UI's active-subagent map
                        # would only be populated lazily from activity events,
                        # which collapses parallel subagents onto one slot.
                        subagent_started_ids: set[str] = set()
                        for p in seg_plans:
                            slot_id = self._subagent_slot_id(p.tool_call)
                            self.print_tool_started(
                                "lead", "subagent", p.tool_call.input, tool_call_id=slot_id
                            )
                            subagent_started_ids.add(slot_id)
                        # Write the tool_call + a running placeholder tool_result
                        # BEFORE dispatch so an interrupt still leaves a visible
                        # (rewritable) record in session.messages for the next
                        # turn's lead to decide whether to resume. See
                        # _append_subagent_placeholders / _finalize_*.
                        _placeholder_assistant, _placeholder_items = (
                            self._append_subagent_placeholders(session, seg_plans, turn=turn)
                        )
                        placeholder_assistant_messages.append(_placeholder_assistant)
                        placeholder_tool_call_ids.update(p.tool_call.id for p in seg_plans)
                        try:
                            summaries = run_parallel_explore_subagents(
                                self.run_subagent,
                                [p.tool_call for p in seg_plans],
                                should_interrupt=should_interrupt,
                                settings=self.settings,
                                session_id=session.id,
                                checkpoint_store=self.subagent_checkpoint_store,
                            )
                        except TurnInterrupted:
                            # Rewrite placeholders to interrupted + resume
                            # pointer BEFORE re-raising; the round-end save and
                            # the _agent_loop interrupt handler's save will then
                            # capture the rewritten state.
                            self._finalize_placeholders_interrupted(_placeholder_items, seg_plans)
                            raise
                        seg_records = [
                            finalize_tool_call(
                                p.tool_call,
                                summary,
                                max_content_chars=self.settings.runtime.max_tool_output_chars,
                            )
                            for p, summary in zip(seg_plans, summaries)
                        ]
                        # Rewrite the placeholder items in place with the real
                        # results; they are already in session.messages.
                        self._finalize_placeholders_completed(_placeholder_items, seg_records)
                    elif run_end - cursor > 1:
                        # Pre-fire ``print_tool_started`` for every parallel-safe
                        # tool BEFORE dispatch so the UI registers each as running
                        # while the pool executes them (mirrors the subagent
                        # branch). The side-effect loop skips the now-redundant
                        # post-execute start for these ids.
                        for p in seg_plans:
                            self.print_tool_started(
                                "lead", p.tool_name, p.tool_call.input, tool_call_id=p.tool_call.id
                            )
                            pre_fired_tool_ids.add(p.tool_call.id)
                        seg_records = dispatch_parallel_segment(
                            self.registry,
                            _lead_ctx,
                            [p.tool_call for p in seg_plans],
                            should_interrupt=should_interrupt,
                            settings=self.settings,
                            max_content_chars=self.settings.runtime.max_tool_output_chars,
                        )
                    else:
                        # Single-call inline branch. For a subagent call (single
                        # Explore or any general-purpose subagent) we also write
                        # a placeholder first so an interrupt leaves a visible,
                        # resumable record -- mirroring the parallel branch.
                        if current.tool_name == "subagent":
                            slot_id = self._subagent_slot_id(current.tool_call)
                            self.print_tool_started(
                                "lead", "subagent", current.tool_call.input, tool_call_id=slot_id
                            )
                            subagent_started_ids = {slot_id}
                            _ph_asst, _ph_items = self._append_subagent_placeholders(session, [current], turn=turn)
                            placeholder_assistant_messages.append(_ph_asst)
                            placeholder_tool_call_ids.add(current.tool_call.id)
                            try:
                                seg_records = [
                                    execute_tool_call(
                                        self.registry,
                                        _lead_ctx(),
                                        current.tool_call,
                                        max_content_chars=self.settings.runtime.max_tool_output_chars,
                                    )
                                ]
                            except TurnInterrupted:
                                self._finalize_placeholders_interrupted(_ph_items, [current])
                                raise
                            self._finalize_placeholders_completed(_ph_items, seg_records)
                        else:
                            self.print_tool_started(
                                "lead", current.tool_name, current.tool_call.input, tool_call_id=current.tool_call.id
                            )
                            pre_fired_tool_ids.add(current.tool_call.id)
                            seg_records = [
                                execute_tool_call(
                                    self.registry,
                                    _lead_ctx(),
                                    current.tool_call,
                                    max_content_chars=self.settings.runtime.max_tool_output_chars,
                                )
                            ]
                    # Apply side effects in input order, breaking on stateful
                    # interrupts exactly like the serial loop.
                    #
                    # ``subagent_started_ids`` is non-None only for the parallel
                    # Explore-subagent branch, which pre-fired ``print_tool_started``
                    # before dispatch. For those calls we skip the redundant
                    # post-completion start event (the slot already exists and was
                    # updated by the subagent's activity events) and instead emit
                    # a finished notification so the UI clears each slot.
                    seg_broke = False
                    for offset, record in enumerate(seg_records):
                        planned = seg_plans[offset]
                        tool_call = planned.tool_call
                        tool_name = planned.tool_name
                        if tool_name == "compress":
                            manual_compact = True
                        if planned.decision == "execute":
                            if subagent_started_ids is not None and self._subagent_slot_id(tool_call) in subagent_started_ids:
                                # Pre-fired above; emit the matching finish so the
                                # UI clears the active-subagent slot now it's done.
                                self._print_tool_finished_subagent(tool_call)
                            elif tool_call.id not in pre_fired_tool_ids:
                                # Not pre-fired before execution; emit the start
                                # event here. Pre-fired normal tools already had
                                # their start emitted before execution, so they
                                # are skipped to avoid a stale duplicate that
                                # would render as a lingering "running" entry.
                                self.print_tool_started("lead", tool_name, tool_call.input, tool_call_id=tool_call.id)
                        if record.repair_hint is not None:
                            pending_tool_repair_hints.append(record.repair_hint)
                        log_id = self.print_tool_event("lead", tool_name, tool_call.input, record.persisted_output)
                        # Placeholder subagent calls already had their
                        # assistant tool_call + tool_result written (and the
                        # tool_result rewritten in place with the real result).
                        # Skip appending them again to executed_tool_calls /
                        # tool_results / transcript to avoid duplicates; the
                        # round-end assistant/tool_result append filters them
                        # out too. UI events and flags still run.
                        if tool_call.id in placeholder_tool_call_ids:
                            if tool_name == "TodoWrite":
                                used_todo = True
                            if planned.is_turn_boundary or self._new_session_pending_for(tool_name, session):
                                end_turn_after_tool = True
                                seg_broke = True
                                break
                            if callable(prepare_next_loop_user_message) and prepare_next_loop_user_message():
                                end_turn_after_tool = True
                                seg_broke = True
                                break
                            if planned.end_turn_after:
                                end_turn_after_tool = True
                                seg_broke = True
                                break
                            continue
                        result = record.result_item
                        result["raw_output"] = record.persisted_output
                        result["log_id"] = log_id
                        executed_tool_calls.append(tool_call)
                        tool_results.append(result)
                        self._append_transcript_entry(
                            session.id,
                            {
                                "role": "tool",
                                "name": tool_name,
                                "input": tool_call.input,
                                "output": result["content"],
                            },
                        )
                        if tool_name == "TodoWrite":
                            used_todo = True
                        if planned.is_turn_boundary or self._new_session_pending_for(tool_name, session):
                            end_turn_after_tool = True
                            seg_broke = True
                            break
                        if callable(prepare_next_loop_user_message) and prepare_next_loop_user_message():
                            end_turn_after_tool = True
                            seg_broke = True
                            break
                        if planned.end_turn_after:
                            end_turn_after_tool = True
                            seg_broke = True
                            break
                    if seg_broke:
                        break
                    cursor = run_end

                # Round-end assistant + tool_result append. Skip both when the
                # round was entirely placeholder subagent calls (their
                # assistant/tool_result pair was already written up front and
                # rewritten in place); otherwise build the message from the
                # non-placeholder tool calls only (placeholder subagent
                # tool_calls already live in their own assistant message).
                if executed_tool_calls or tool_results:
                    assistant_message = turn.as_message(executed_tool_calls)
                    assistant_message = self._attach_thinking_log_marker(
                        assistant_message,
                        thinking_log=thinking_log,
                        thinking_callback=thinking_callback,
                        notify_finished=not thinking_finished_notified,
                    )
                    session.messages.append(assistant_message)
                    self._append_transcript_entry(session.id, assistant_message)
                    session.rounds_without_todo = 0 if used_todo else session.rounds_without_todo + 1
                    tool_result_message = make_tool_result_message(tool_results)
                    session.messages.append(tool_result_message)
                else:
                    # Placeholder-only round: the assistant text (if any) from
                    # the model turn would be lost without a transcript note,
                    # but placeholder subagent rounds carry no user-facing text
                    # (the model only emitted tool_calls). Nothing to append.
                    session.rounds_without_todo = 0 if used_todo else session.rounds_without_todo + 1
                if manual_compact:
                    preserve_from_index = self._active_task_preserve_index(session.messages, task_anchor_message)
                    session.messages = self.compact_manager.auto_compact(
                        session.id,
                        session.messages,
                        preserve_from_index=preserve_from_index,
                    )
                    self._record_session_token_usage(session, getattr(self.compact_manager, "last_usage", None))
                    self._note_context_governance(session.id, "manual_compact", "auto-compacted session history")
                    try:
                        self.context_window_usage(session)
                    except Exception:
                        pass
                self.session_manager.save(session)
                if session.id in self._new_session_pendings():
                    # request_new_session queued a swap: end the turn hard, right
                    # after the tool result is persisted, instead of starting
                    # another round the fresh session would never see.
                    return self._agent_loop_result(
                        final_text or "Session swap queued; continuing in a fresh session.",
                        status="completed",
                        session=session,
                    )
                if pending_todo_reconcile and used_todo:
                    if executed_tool_calls and all(tool_call.name == "TodoWrite" for tool_call in executed_tool_calls):
                        if not self.todo_manager.has_open_items(session):
                            return self._agent_loop_result(final_text, status="completed", session=session)
                        pending_todo_reconcile = False
                        continue
                    pending_todo_reconcile = False
                if end_turn_after_tool:
                    continue
            self._capture_turn_file_changes(session)
            self.session_manager.save(session)
            open_todo_count = self._count_open_todo_items(session)
            if open_todo_count > 0:
                return AgentLoopResult(
                    final_text
                    or (
                        f"Stopped after max rounds with open todo items remaining ({open_todo_count} open). "
                        "Continue the session to resume unfinished work."
                    ),
                    status="stopped_with_open_todos",
                    open_todo_count=open_todo_count,
                )
            return self._agent_loop_result(
                final_text or "Stopped after max rounds.",
                status="stopped_after_max_rounds",
                session=session,
            )
        except TurnInterrupted:
            self.interrupt_active_teammates(reason="lead_interrupt")
            session.pending_file_changes = []
            session.last_turn_file_changes = []
            self.session_manager.save(session)
            raise
        except Exception as exc:
            try:
                self._hook_manager().on_turn_failed(
                    session=session,
                    trace_id=f"{session.id}-{getattr(session, 'latest_turn_id', None) or 'failed'}",
                    actor="lead",
                    execution_mode=getattr(self, "execution_mode", DEFAULT_EXECUTION_MODE),
                    error=exc,
                )
            except Exception:
                pass
            raise

    def doctor(self) -> str:
        lines = [
            f"workspace: {self.settings.workspace_root}",
            f"provider: {self.settings.provider.name}",
            f"model: {self.settings.provider.model}",
            f"api_key_configured: {'yes' if self.settings.provider.api_key else 'no'}",
            f"configured_providers: {', '.join(sorted(self.settings.provider_profiles))}",
            f"skills_dir: {'present' if (self.settings.workspace_root / 'skills').exists() else 'missing'}",
            f"data_dir: {self.settings.storage.data_dir}",
            f"state_dir: {self.settings.storage.state_dir}",
        ]
        if self.settings.mcp_servers:
            lines.append("mcp:")
            lines.extend(f"  {line}" for line in self.mcp_registry.status_lines())
        else:
            lines.append("mcp: none configured")
        return "\n".join(lines)

    def mcp_status(self) -> str:
        return self.mcp_registry.describe_servers()

    def close(self) -> None:
        self.mcp_registry.close()
