"""Agent 运行时模块.

提供 OpenAgent 的核心运行时功能，包括：
- LLM 提供者管理
- 工具注册和执行
- 会话管理
- 子代理运行
- 后台任务管理
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

from open_somnia.collaboration.bus import MessageBus
from open_somnia.collaboration.protocols import RequestTracker
from open_somnia.config.models import AppSettings, HookSettings, ProviderProfileSettings, ProviderSettings
from open_somnia.config.settings import _materialize_provider, load_settings, persist_hook_enabled, persist_provider_selection
from open_somnia.config.settings import BUILTIN_NOTIFY_MANAGER
from open_somnia.hooks.manager import HookManager
from open_somnia.mcp.registry import MCPRegistry
from open_somnia.providers.anthropic_provider import AnthropicProvider
from open_somnia.providers.base import LLMProvider, ProviderError
from open_somnia.providers.openai_provider import OpenAIProvider
from open_somnia.runtime.compact import (
    AUTO_COMPACT_TRIGGER_RATIO,
    CompactManager,
    ContextWindowUsage,
    SEMANTIC_JANITOR_TRIGGER_RATIO,
    SemanticCompressionDecision,
    ToolResultCandidate,
    build_payload_messages,
    estimate_payload_tokens,
    extract_tool_result_candidates,
    persist_semantic_compression,
    should_auto_compact,
    should_run_semantic_janitor,
)
from open_somnia.runtime.execution_mode import (
    AUTHORIZATION_TOOL_NAME,
    DEFAULT_EXECUTION_MODE,
    MODE_SWITCH_TOOL_NAME,
    NON_YOLO_EXECUTION_MODES,
)
from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import make_tool_result_message, make_user_text_message, render_text_content
from open_somnia.runtime.permissions import PermissionManager
from open_somnia.runtime.session import AgentSession, SessionManager
from open_somnia.runtime.subagent_runner import SubagentRunner
from open_somnia.runtime.system_prompt import SystemPromptBuilder
from open_somnia.runtime.teammate import TeammateRuntimeManager
from open_somnia.runtime.tool_events import ToolEventRenderer
from open_somnia.skills.loader import SkillLoader
from open_somnia.storage.inbox import InboxStore
from open_somnia.storage.jobs import JobStore
from open_somnia.storage.sessions import SessionStore
from open_somnia.storage.common import atomic_write_text
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
from open_somnia.tools.todo import TodoManager, register_todo_tool


class OpenAgentRuntime:
    TOOL_VALUE_PREVIEW_CHARS = 90
    TOOL_RESULT_PREVIEW_CHARS = 60
    SILENT_TOOL_NAMES = {"TodoWrite"}
    MAX_UNDO_TURNS = 10
    TURN_BOUNDARY_TOOL_NAMES = {AUTHORIZATION_TOOL_NAME, MODE_SWITCH_TOOL_NAME}
    WORKSPACE_PERMISSIONS_FILE = "permissions.json"
    PROVIDER_POLL_INTERVAL_SECONDS = 0.1
    JANITOR_REARM_RATIO = 0.45
    JANITOR_FORCE_RATIO = 0.60
    JANITOR_MIN_TOKEN_DELTA = 8_000
    JANITOR_MIN_MESSAGE_DELTA = 6
    MANUAL_JANITOR_MIN_RATIO = 0.20
    JANITOR_MIN_USAGE_DELTA_RATIO = 0.05
    JANITOR_MIN_USAGE_DELTA_TOKENS = 1_000
    JANITOR_MIN_PRUNABLE_CANDIDATES = 1
    JANITOR_PRUNABLE_OUTPUT_CHARS = 240
    JANITOR_LOW_YIELD_RATIO = 0.05
    JANITOR_LOW_YIELD_MAX_AUTO_RUNS = 2
    JANITOR_PREEMPTIVE_COMPACT_GAP = 0.02
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
        self.permission_manager = PermissionManager(self)
        self.subagent_runner = SubagentRunner(self)
        self.system_prompt_builder = SystemPromptBuilder(self)
        self._workspace_authorized_tools = self._load_workspace_authorizations()
        self._once_authorized_tools: dict[str, int] = {}
        self.provider = self._make_provider()
        self.transcript_store = TranscriptStore(settings.storage.transcripts_dir)
        self.session_manager = SessionManager(SessionStore(settings.storage.sessions_dir), self.transcript_store)
        self.task_store = TaskStore(settings.storage.tasks_dir)
        self.job_store = JobStore(settings.storage.jobs_dir)
        self.tool_log_store = ToolLogStore(settings.storage.logs_dir)
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
        self._janitor_state: dict[str, dict[str, Any]] = {}
        self._current_working_file: dict[str, Any] | None = None
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
        if provider_settings.provider_type == "openai":
            return OpenAIProvider(provider_settings)
        return AnthropicProvider(provider_settings)

    def configured_provider_profiles(self) -> dict[str, ProviderProfileSettings]:
        return dict(self.settings.provider_profiles)

    def configured_hooks(self) -> list[HookSettings]:
        return list(getattr(self.settings, "hooks", []) or [])

    def _workspace_authorizations_path(self) -> Path | None:
        return self._permission_manager().workspace_authorizations_path()

    def _load_workspace_authorizations(self) -> set[str]:
        return self._permission_manager().load_workspace_authorizations()

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

    def switch_provider_model(self, provider_name: str, model: str) -> str:
        normalized_provider = provider_name.strip().lower()
        normalized_model = model.strip()
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
        self._janitor_state = {}
        persist_provider_selection(self.settings, normalized_provider, normalized_model)
        return (
            f"Switched to provider '{self.settings.provider.name}' with model "
            f"'{self.settings.provider.model}' and saved it to .open_somnia/open_somnia.toml."
        )

    def reload_provider_configuration(self, *, provider_name: str | None = None, model: str | None = None) -> None:
        provider_override = provider_name or self.settings.provider.name
        model_override = model or self.settings.provider.model
        reloaded = load_settings(
            self.settings.workspace_root,
            provider_override=provider_override,
            model_override=model_override,
        )
        self.settings.provider_profiles = reloaded.provider_profiles
        self.settings.provider = reloaded.provider
        self.settings.raw_config = reloaded.raw_config
        self.provider = self._instantiate_provider(self.settings.provider)
        self.compact_manager.provider = self.provider
        self.compact_manager.model_max_tokens = self.settings.provider.max_tokens
        self._context_usage_cache = {}
        self._payload_message_cache = {}
        self._recent_context_usage = {}
        self._janitor_state = {}

    def reload_hook_configuration(self) -> None:
        reloaded = load_settings(
            self.settings.workspace_root,
            provider_override=self.settings.provider.name,
            model_override=self.settings.provider.model,
        )
        self.settings.raw_config = reloaded.raw_config
        self.settings.hooks = reloaded.hooks
        self.hook_manager = HookManager(self.settings)

    def set_hook_enabled(self, hook: HookSettings, enabled: bool) -> str:
        config_path = persist_hook_enabled(hook, enabled)
        self.reload_hook_configuration()
        state = "enabled" if enabled else "disabled"
        kind = "builtin" if hook.managed_by == BUILTIN_NOTIFY_MANAGER else "custom"
        scope = getattr(hook, "config_scope", None) or "config"
        return f"{state.capitalize()} {kind} hook for {hook.event} in {scope} config: {config_path}"

    def _context_usage_tools(self, actor: str) -> list[dict[str, Any]]:
        registry = self.registry if actor == "lead" else self.worker_registry
        return registry.schemas()

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

    def _janitor_state_for(self, session: AgentSession | None) -> dict[str, Any] | None:
        if session is None:
            return None
        session_id = str(getattr(session, "id", "")).strip()
        if not session_id:
            return None
        states = getattr(self, "_janitor_state", None)
        if states is None:
            states = {}
            self._janitor_state = states
        return states.setdefault(
            session_id,
            {
                "armed": True,
                "last_run_used_tokens": 0,
                "last_run_message_count": 0,
                "last_run_ratio": 0.0,
                "last_reduction_ratio": 0.0,
                "saturated": False,
                "auto_low_yield_streak": 0,
                "disabled": False,
            },
        )

    def _record_context_janitor_run(
        self,
        session: AgentSession | None,
        before_usage: ContextWindowUsage,
        after_usage: ContextWindowUsage,
        *,
        message_count: int,
        automatic: bool,
    ) -> None:
        state = self._janitor_state_for(session)
        if state is None:
            return
        before_tokens = max(0, int(before_usage.used_tokens or 0))
        after_tokens = max(0, int(after_usage.used_tokens or 0))
        reduction_ratio = 0.0
        if before_tokens > 0 and after_tokens <= before_tokens:
            reduction_ratio = max(0.0, (before_tokens - after_tokens) / before_tokens)
        state["armed"] = False
        state["last_run_used_tokens"] = after_tokens
        state["last_run_message_count"] = max(0, int(message_count))
        state["last_run_ratio"] = float(after_usage.usage_ratio or 0.0)
        state["last_reduction_ratio"] = reduction_ratio
        state["saturated"] = False
        if automatic:
            if reduction_ratio < self.JANITOR_LOW_YIELD_RATIO:
                state["auto_low_yield_streak"] = int(state.get("auto_low_yield_streak") or 0) + 1
                if state["auto_low_yield_streak"] >= self.JANITOR_LOW_YIELD_MAX_AUTO_RUNS:
                    state["disabled"] = True
            else:
                state["auto_low_yield_streak"] = 0

    def _should_run_manual_context_janitor(self, usage: ContextWindowUsage) -> bool:
        ratio = usage.usage_ratio
        return ratio is not None and ratio >= self.MANUAL_JANITOR_MIN_RATIO

    def _semantic_janitor_trigger_ratio(self) -> float:
        return float(SEMANTIC_JANITOR_TRIGGER_RATIO)

    def _janitor_preemptive_compact_ratio(self) -> float:
        return max(self._semantic_janitor_trigger_ratio(), AUTO_COMPACT_TRIGGER_RATIO - self.JANITOR_PREEMPTIVE_COMPACT_GAP)

    def _janitor_candidates(self, messages: list[dict[str, Any]]) -> list[ToolResultCandidate]:
        return extract_tool_result_candidates(messages, preserve_recent_rounds=2)

    def _selected_janitor_candidates(self, messages: list[dict[str, Any]]) -> list[ToolResultCandidate]:
        candidates = self._janitor_candidates(messages)
        if not candidates:
            return []
        selected = sorted(candidates, key=lambda item: (item.output_length, item.age), reverse=True)[:12]
        selected.sort(key=lambda item: (item.locator.message_index, item.locator.item_index))
        return selected

    def _count_prunable_janitor_candidates(self, messages: list[dict[str, Any]]) -> int:
        return sum(1 for candidate in self._janitor_candidates(messages) if candidate.output_length >= self.JANITOR_PRUNABLE_OUTPUT_CHARS)

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
            return build_payload_messages(messages)
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

        payload_messages = build_payload_messages(messages)
        baseline_usage = self._count_payload_usage(system_prompt, payload_messages, tools)
        message_count = len(messages)
        final_usage = baseline_usage
        if self._should_run_context_janitor(
            baseline_usage,
            session=session,
            message_count=message_count,
            messages=messages,
        ):
            decisions = self._analyze_context_relevance(
                session=session,
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
            )
            if decisions:
                changed_results = sum(1 for decision in decisions if decision.state != "original")
                persist_semantic_compression(messages, decisions)
                payload_messages = build_payload_messages(messages, semantic_decisions=decisions)
                final_usage = self._count_payload_usage(system_prompt, payload_messages, tools)
                if changed_results > 0:
                    self._note_context_governance(
                        session.id,
                        "janitor",
                        f"janitor reduced {changed_results} tool result(s)",
                    )
            self._record_context_janitor_run(
                session,
                baseline_usage,
                final_usage,
                message_count=message_count,
                automatic=True,
            )
        usage_cache[session.id] = (cache_key, final_usage)
        self._remember_context_usage(session.id, final_usage)
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
            source = str(usage.get("source", "provider"))
            if total_tokens > 0:
                return {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "source": source,
                }

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

    def _ensure_session_token_usage(self, session: AgentSession) -> dict[str, int]:
        usage = getattr(session, "token_usage", None)
        if not isinstance(usage, dict):
            usage = {}
            session.token_usage = usage
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            usage[key] = int(usage.get(key) or 0)
        return usage

    def _record_session_token_usage(self, session: AgentSession, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        totals = self._ensure_session_token_usage(session)
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)

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

    def _should_run_context_janitor(
        self,
        usage: ContextWindowUsage,
        *,
        session: AgentSession | None = None,
        message_count: int | None = None,
        messages: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> bool:
        ratio = usage.usage_ratio
        if ratio is None:
            return False
        state = self._janitor_state_for(session)
        if not should_run_semantic_janitor(usage):
            if state is not None and ratio <= self.JANITOR_REARM_RATIO:
                state["armed"] = True
                state["saturated"] = False
            return False
        if state is not None and bool(state.get("disabled")):
            return False
        if not force and ratio >= self._janitor_preemptive_compact_ratio():
            if state is not None:
                state["saturated"] = False
            return False
        if messages is not None:
            prunable_count = self._count_prunable_janitor_candidates(messages)
            if state is not None:
                state["saturated"] = prunable_count == 0
            if prunable_count < self.JANITOR_MIN_PRUNABLE_CANDIDATES:
                return False
        if force or session is None or state is None:
            return True
        last_used_tokens = int(state.get("last_run_used_tokens") or 0)
        last_ratio = float(state.get("last_run_ratio") or 0.0)
        token_delta = max(0, usage.used_tokens - last_used_tokens)
        ratio_delta = max(0.0, ratio - last_ratio)
        if last_used_tokens > 0 and token_delta < self.JANITOR_MIN_USAGE_DELTA_TOKENS and ratio_delta < self.JANITOR_MIN_USAGE_DELTA_RATIO:
            return False
        if bool(state.get("armed", True)):
            return True
        if ratio >= self.JANITOR_FORCE_RATIO:
            return True
        last_message_count = int(state.get("last_run_message_count") or 0)
        if token_delta >= self.JANITOR_MIN_TOKEN_DELTA:
            return True
        if max(0, int(message_count or 0) - last_message_count) >= self.JANITOR_MIN_MESSAGE_DELTA:
            return True
        return False

    def _context_compact_text(self, text: str, *, limit: int = 220) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 3)] + "..."

    def _extract_topic_tokens(self, text: str) -> set[str]:
        stopwords = {
            "the",
            "and",
            "that",
            "with",
            "from",
            "this",
            "into",
            "have",
            "need",
            "when",
            "then",
            "than",
            "were",
            "been",
            "about",
            "after",
            "before",
            "using",
            "used",
            "user",
            "assistant",
            "tool",
            "result",
            "output",
            "current",
            "should",
            "would",
            "could",
            "there",
            "their",
            "them",
            "file",
            "files",
            "line",
            "lines",
        }
        return {
            token.lower()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
            if token.lower() not in stopwords
        }

    def _extract_recent_topic_context(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        visible: list[dict[str, str]] = []
        for message in reversed(messages):
            if len(visible) >= 4:
                break
            if not self._is_visible_conversation_message(message):
                continue
            text = render_text_content(message.get("content", ""))
            compact = self._context_compact_text(text, limit=400)
            if not compact:
                continue
            visible.append({"role": str(message.get("role", "user")), "text": compact})
        visible.reverse()
        combined = "\n".join(f"{item['role']}: {item['text']}" for item in visible)
        active_files = sorted(
            {
                match
                for match in re.findall(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+", combined)
                if "." in match and len(match) > 2
            }
        )[:8]
        symbol_candidates = {
            token
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", combined)
            if token.lower()
            not in {
                "user",
                "assistant",
                "error",
                "output",
                "current",
                "context",
                "please",
                "also",
                "check",
                "compare",
                "inspect",
            }
        }
        active_symbols = sorted(
            symbol_candidates,
            key=lambda token: (
                0 if ("_" in token or any(char.isupper() for char in token[1:])) else 1,
                token.lower(),
            ),
        )[:12]
        keywords = sorted(self._extract_topic_tokens(combined))[:18]
        return {
            "conversation_excerpt": combined,
            "active_files": active_files,
            "active_symbols": active_symbols,
            "keywords": keywords,
        }

    def _todo_hint_context(self, session: AgentSession) -> dict[str, Any]:
        open_items: list[str] = []
        completed_items: list[str] = []
        open_tokens: set[str] = set()
        completed_tokens: set[str] = set()
        for item in getattr(session, "todo_items", []) or []:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            status = str(item.get("status", "pending")).lower()
            if status in {"pending", "in_progress"}:
                open_items.append(content)
                open_tokens.update(self._extract_topic_tokens(content))
            elif status == "completed":
                completed_items.append(content)
                completed_tokens.update(self._extract_topic_tokens(content))
        return {
            "open_items": open_items[:6],
            "completed_items": completed_items[:6],
            "open_tokens": open_tokens,
            "completed_tokens": completed_tokens,
        }

    def _tool_candidate_haystack(self, candidate: ToolResultCandidate) -> str:
        tool_input = json.dumps(candidate.tool_input, ensure_ascii=False, default=str)
        return " ".join(
            part for part in (candidate.tool_name, tool_input, candidate.content, candidate.output_preview) if part
        ).lower()

    def _candidate_target_path(self, candidate: ToolResultCandidate) -> str:
        path = candidate.tool_input.get("path")
        if path is None:
            return ""
        return str(path).strip().replace("\\", "/").lower()

    def _render_condensed_context(self, candidate: ToolResultCandidate, summary: str | None) -> str:
        prefix = f"[Semantic Summary | {candidate.tool_name}"
        if candidate.log_id:
            prefix += f" | log {candidate.log_id}"
        prefix += "]"
        body = self._context_compact_text(summary or candidate.output_preview or "Relevant prior tool output reviewed earlier.", limit=260)
        return f"{prefix} {body}".strip()

    def _render_evicted_context(self, candidate: ToolResultCandidate) -> str:
        prefix = f"[Context Evicted | {candidate.tool_name}"
        if candidate.log_id:
            prefix += f" | log {candidate.log_id}"
        prefix += "]"
        return f"{prefix} Output removed from payload. Use request_original_context if needed."

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
        if candidate.tool_name in {"read_file", "find_symbol", "read_text"}:
            score += 2
        if candidate.tool_name in {"pwd", "cd", "ls", "tree", "glob"}:
            score -= 3
        if candidate.age >= 6:
            score -= 1
        if candidate.age >= 10:
            score -= 1
        return score

    def _fallback_context_relevance_decisions(
        self,
        session: AgentSession,
        candidates: list[ToolResultCandidate],
        topic_context: dict[str, Any],
    ) -> list[SemanticCompressionDecision]:
        todo_context = self._todo_hint_context(session)
        active_files = {value.lower() for value in topic_context.get("active_files", [])}
        active_symbols = {value.lower() for value in topic_context.get("active_symbols", [])}
        topic_tokens = {value.lower() for value in topic_context.get("keywords", [])}
        latest_snapshot_by_path: dict[str, ToolResultCandidate] = {}
        for candidate in sorted(candidates, key=lambda item: (item.locator.message_index, item.locator.item_index)):
            candidate_path = self._candidate_target_path(candidate)
            if candidate_path and candidate.tool_name in {"read_file", "write_file", "edit_file"}:
                latest_snapshot_by_path[candidate_path] = candidate
        decisions: list[SemanticCompressionDecision] = []
        for candidate in candidates:
            candidate_path = self._candidate_target_path(candidate)
            latest_snapshot = latest_snapshot_by_path.get(candidate_path) if candidate_path else None
            if candidate.tool_name == "read_file" and latest_snapshot is not None and latest_snapshot.locator != candidate.locator:
                decisions.append(
                    SemanticCompressionDecision(
                        message_index=candidate.locator.message_index,
                        item_index=candidate.locator.item_index,
                        state="evicted",
                        summary=self._render_evicted_context(candidate),
                    )
                )
                continue
            if latest_snapshot is not None and latest_snapshot.locator == candidate.locator and candidate.tool_name in {"read_file", "write_file", "edit_file"}:
                decisions.append(
                    SemanticCompressionDecision(
                        message_index=candidate.locator.message_index,
                        item_index=candidate.locator.item_index,
                        state="original",
                        summary=None,
                    )
                )
                continue
            score = self._candidate_relevance_score(
                candidate,
                active_files=active_files,
                active_symbols=active_symbols,
                topic_tokens=topic_tokens,
                open_todo_tokens={value.lower() for value in todo_context["open_tokens"]},
                completed_todo_tokens={value.lower() for value in todo_context["completed_tokens"]},
            )
            if candidate.has_error or score >= 5:
                state = "original"
                summary = None
            elif score >= 2 or candidate.output_length >= 900 or candidate.tool_name in {"grep", "bash"}:
                state = "condensed"
                summary = self._render_condensed_context(candidate, None)
            elif candidate.tool_name in {"pwd", "cd", "ls", "tree", "glob"} and candidate.age >= 2:
                state = "evicted"
                summary = self._render_evicted_context(candidate)
            else:
                state = "condensed"
                summary = self._render_condensed_context(candidate, None)
            decisions.append(
                SemanticCompressionDecision(
                    message_index=candidate.locator.message_index,
                    item_index=candidate.locator.item_index,
                    state=state,
                    summary=summary,
                )
            )
        return decisions

    def _strip_json_fence(self, text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
        return stripped.strip()

    def _parse_semantic_janitor_response(
        self,
        text: str,
        candidates: list[ToolResultCandidate],
    ) -> list[SemanticCompressionDecision]:
        cleaned = self._strip_json_fence(text)
        payload = json.loads(cleaned)
        if not isinstance(payload, list):
            raise ValueError("Semantic janitor response must be a JSON list.")
        candidates_by_locator = {candidate.locator: candidate for candidate in candidates}
        decisions: list[SemanticCompressionDecision] = []
        seen: set[tuple[int, int]] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            message_index = int(item.get("message_index"))
            item_index = int(item.get("item_index"))
            locator = (message_index, item_index)
            if locator in seen:
                continue
            candidate = candidates_by_locator.get(
                SemanticCompressionDecision(
                    message_index=message_index,
                    item_index=item_index,
                    state="original",
                ).locator
            )
            if candidate is None:
                continue
            state = str(item.get("state", "original")).strip().lower()
            if state not in {"original", "condensed", "evicted"}:
                continue
            summary_text = str(item.get("summary", "")).strip()
            summary: str | None = None
            if state == "condensed":
                summary = self._render_condensed_context(candidate, summary_text or None)
            elif state == "evicted":
                summary = self._render_evicted_context(candidate)
            decisions.append(
                SemanticCompressionDecision(
                    message_index=message_index,
                    item_index=item_index,
                    state=state,
                    summary=summary,
                )
            )
            seen.add(locator)
        return decisions

    def _build_semantic_janitor_prompt(
        self,
        topic_context: dict[str, Any],
        todo_context: dict[str, Any],
        candidates: list[ToolResultCandidate],
    ) -> str:
        topic_lines = [
            "Current recent topic:",
            f"- Conversation excerpt: {topic_context.get('conversation_excerpt', '(none)') or '(none)'}",
            f"- Active files: {', '.join(topic_context.get('active_files', [])) or '(none)'}",
            f"- Active symbols: {', '.join(topic_context.get('active_symbols', [])) or '(none)'}",
            f"- Keywords: {', '.join(topic_context.get('keywords', [])) or '(none)'}",
            "",
            "Todo hints:",
            f"- Open items: {', '.join(todo_context.get('open_items', [])) or '(none)'}",
            f"- Completed items: {', '.join(todo_context.get('completed_items', [])) or '(none)'}",
            "",
            "Candidate tool results:",
        ]
        for candidate in candidates:
            topic_lines.extend(
                [
                    f"- message_index={candidate.locator.message_index} item_index={candidate.locator.item_index} tool={candidate.tool_name} age={candidate.age}",
                    f"  log_id={candidate.log_id or '(none)'}",
                    f"  input={self._context_compact_text(json.dumps(candidate.tool_input, ensure_ascii=False, default=str), limit=180)}",
                    f"  output_preview={candidate.output_preview or '(no output)'}",
                    f"  output_length={candidate.output_length}",
                ]
            )
        topic_lines.extend(
            [
                "",
                "Return strict JSON only.",
                "Each item must contain message_index, item_index, state.",
                "Allowed states: original, condensed, evicted.",
                "Include summary only when state is condensed.",
            ]
        )
        return "\n".join(topic_lines)

    def _analyze_context_relevance(
        self,
        *,
        session: AgentSession,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> list[SemanticCompressionDecision]:
        del system_prompt, tools
        selected = self._selected_janitor_candidates(messages)
        if not selected:
            return []
        topic_context = self._extract_recent_topic_context(messages)
        todo_context = self._todo_hint_context(session)
        fallback = self._fallback_context_relevance_decisions(session, selected, topic_context)
        try:
            turn = self.provider.complete(
                system_prompt=(
                    "You are a context janitor for a coding agent.\n"
                    "Prioritize the current recent topic. Todo items are only weak hints.\n"
                    "Decide whether each old tool result should remain original, be condensed into one factual sentence, or be evicted.\n"
                    "Return strict JSON only."
                ),
                messages=[{"role": "user", "content": self._build_semantic_janitor_prompt(topic_context, todo_context, selected)}],
                tools=[],
                max_tokens=min(900, self.settings.provider.max_tokens),
            )
            text = "\n".join(getattr(turn, "text_blocks", []) or []).strip()
            if not text:
                return fallback
            parsed = self._parse_semantic_janitor_response(text, selected)
            return parsed or fallback
        except Exception:
            return fallback

    def request_original_context(self, log_id: str) -> str:
        normalized_log_id = str(log_id).strip()
        if not normalized_log_id:
            return "log_id is required."
        entry = self.tool_log_store.get(normalized_log_id)
        if not entry:
            return f"No tool log found for '{normalized_log_id}'."
        tool_name = str(entry.get("tool_name", "tool")).strip() or "tool"
        output = str(entry.get("output", ""))
        return f"[Restored tool output | {tool_name} | log {normalized_log_id}]\n{output or '(no output)'}"

    def _register_core_tools(self, registry: ToolRegistry) -> None:
        register_shell_tool(registry)
        register_filesystem_tools(registry)
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
        register_task_tools(registry, self.task_store)
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
                    "Request that the user switch execution mode to shortcuts, plan, or accept_edits only."
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
                name="compress",
                description="Manually compact the current conversation context.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: "Compressing...",
            )
        )
        registry.register(
            ToolDefinition(
                name="request_original_context",
                description="Reload the full original output for a prior tool result by log id.",
                input_schema={
                    "type": "object",
                    "properties": {"log_id": {"type": "string"}},
                    "required": ["log_id"],
                },
                handler=lambda ctx, payload: self.request_original_context(payload["log_id"]),
            )
        )

    def _register_worker_local_tools(self, registry: ToolRegistry) -> None:
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
                handler=lambda ctx, payload: self.bus.send(ctx.actor, payload["to"], payload["content"]),
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

    def _submit_plan(self, actor: str, plan: str) -> str:
        request = self.request_tracker.create_plan_request(actor, plan)
        self.bus.send(actor, "lead", plan, "plan_request", {"request_id": request["request_id"]})
        return f"Submitted plan request {request['request_id']}"

    def _environment_guidance(self) -> str:
        return self._system_prompt_builder().environment_guidance()

    def build_system_prompt(
        self,
        actor: str = "lead",
        role: str = "lead coding agent",
        session: AgentSession | None = None,
    ) -> str:
        return self._system_prompt_builder().build_system_prompt(actor=actor, role=role, session=session)

    def _base_system_prompt(self) -> str:
        return self._system_prompt_builder().base_system_prompt()

    def create_session(self) -> AgentSession:
        self._current_working_file = None
        session = self.session_manager.create()
        self._hook_manager().on_session_start(session)
        return session

    def latest_session(self) -> AgentSession:
        self._current_working_file = None
        return self.session_manager.latest_or_create()

    def load_session(self, session_id: str) -> AgentSession:
        self._current_working_file = None
        return self.session_manager.load(session_id)

    def list_sessions(self) -> list[AgentSession]:
        return self.session_manager.list_all()

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

    def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        text_callback=None,
        should_interrupt=None,
    ):
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(1, 4):
            attempts = attempt
            self._raise_if_interrupted(should_interrupt)
            try:
                if should_interrupt is None:
                    return self.provider.complete(
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=self.settings.provider.max_tokens,
                        text_callback=text_callback,
                        stop_checker=None,
                    )
                return self._complete_with_interrupt_polling(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                    text_callback=text_callback,
                    should_interrupt=should_interrupt,
                )
            except TurnInterrupted:
                raise
            except ProviderError as exc:
                last_error = exc
                if not getattr(exc, "retryable", True):
                    break
            except Exception as exc:
                last_error = exc
                break
        if last_error is None:
            raise RuntimeError("Provider call failed.")
        if attempts <= 1:
            raise RuntimeError(f"Provider call failed: {last_error}")
        raise RuntimeError(f"Provider call failed after {attempts} attempts: {last_error}")

    def _complete_with_interrupt_polling(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        text_callback=None,
        should_interrupt=None,
    ):
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
                turn = self.provider.complete(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=self.settings.provider.max_tokens,
                    text_callback=interruptible_callback if (text_callback is not None or should_interrupt is not None) else text_callback,
                    stop_checker=provider_stop_checker,
                )
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

    def run_subagent(self, prompt: str, agent_type: str = "Explore") -> str:
        return self._subagent_runner().run_subagent(prompt, agent_type)

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

    def run_semantic_janitor(
        self,
        session: AgentSession,
        *,
        actor: str = "lead",
        role: str = "lead coding agent",
    ) -> str:
        messages = getattr(session, "messages", None)
        if not isinstance(messages, list) or not messages:
            return "Janitor skipped: no conversation history."
        try:
            system_prompt = self.build_system_prompt(actor=actor, role=role, session=session)
        except TypeError:
            system_prompt = self.build_system_prompt()
        tools = self._context_usage_tools(actor)
        cache_key = self._payload_message_cache_key(
            session,
            actor=actor,
            role=role,
            system_prompt=system_prompt,
            tools=tools,
        )
        payload_messages = build_payload_messages(messages)
        baseline_usage = self._count_payload_usage(system_prompt, payload_messages, tools)
        if not self._should_run_manual_context_janitor(baseline_usage):
            self._payload_message_cache[session.id] = (cache_key, payload_messages)
            self._context_usage_cache[session.id] = (cache_key, baseline_usage)
            self._remember_context_usage(session.id, baseline_usage)
            usage_label = (
                f"{baseline_usage.usage_percent:.1f}%"
                if baseline_usage.usage_percent is not None
                else f"{baseline_usage.used_tokens} tokens"
            )
            return (
                f"Janitor skipped: current payload usage is {usage_label}, "
                f"below the manual {self.MANUAL_JANITOR_MIN_RATIO * 100:.0f}% trigger."
            )

        decisions = self._analyze_context_relevance(
            session=session,
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
        )
        changed_results = sum(1 for decision in decisions if decision.state != "original")
        if decisions:
            persist_semantic_compression(messages, decisions)
            payload_messages = build_payload_messages(messages, semantic_decisions=decisions)
        reduced_usage = self._count_payload_usage(system_prompt, payload_messages, tools)
        self._payload_message_cache[session.id] = (cache_key, payload_messages)
        self._context_usage_cache[session.id] = (cache_key, reduced_usage)
        self._remember_context_usage(session.id, reduced_usage)
        self._record_context_janitor_run(
            session,
            baseline_usage,
            reduced_usage,
            message_count=len(messages),
            automatic=False,
        )
        if changed_results > 0:
            self._note_context_governance(session.id, "janitor", f"janitor reduced {changed_results} tool result(s)")
        saver = getattr(getattr(self, "session_manager", None), "save", None)
        if callable(saver) and decisions:
            saver(session)
        before_label = (
            f"{baseline_usage.usage_percent:.1f}%"
            if baseline_usage.usage_percent is not None
            else f"{baseline_usage.used_tokens} tokens"
        )
        after_label = (
            f"{reduced_usage.usage_percent:.1f}%"
            if reduced_usage.usage_percent is not None
            else f"{reduced_usage.used_tokens} tokens"
        )
        return (
            f"Janitor reviewed {len(decisions)} candidate tool result(s), reduced {changed_results}, "
            f"and lowered payload usage from {before_label} to {after_label}."
        )

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

    def run_turn(self, session: AgentSession, user_input: str, text_callback=None, should_interrupt=None) -> str:
        session.pending_file_changes = []
        session.last_turn_file_changes = []
        task_anchor_message = make_user_text_message(user_input)
        session.messages.append(task_anchor_message)
        self.transcript_store.append(session.id, {"role": "user", "content": user_input})
        return self._agent_loop(
            session,
            text_callback=text_callback,
            should_interrupt=should_interrupt,
            task_anchor_message=task_anchor_message,
        )

    def _agent_loop(self, session: AgentSession, text_callback=None, should_interrupt=None, task_anchor_message=None) -> str:
        final_text = ""
        try:
            for _ in range(self.settings.runtime.max_agent_rounds):
                self._raise_if_interrupted(should_interrupt)
                background_notifications = self.background_manager.drain()
                if background_notifications:
                    text = "\n".join(
                        f"[bg:{item['task_id']}] {item['status']}: {item['result']}" for item in background_notifications
                    )
                    session.messages.append(make_user_text_message(f"<background-results>\n{text}\n</background-results>"))
                inbox = self.bus.read_inbox("lead")
                if inbox:
                    session.messages.append(make_user_text_message(f"<inbox>{json.dumps(inbox, ensure_ascii=False, indent=2)}</inbox>"))
                if should_auto_compact(
                    self.context_window_usage(session),
                    hard_threshold=self.settings.runtime.token_threshold,
                ):
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

                stream_flush_callback = getattr(text_callback, "finish", None) if text_callback is not None else None
                try:
                    system_prompt = self.build_system_prompt(session=session)
                except TypeError:
                    system_prompt = self.build_system_prompt()
                tool_schemas = self.registry.schemas()
                payload_messages = self._messages_for_model(
                    session.messages,
                    session=session,
                    system_prompt=system_prompt,
                    tools=tool_schemas,
                )

                turn = self.complete(
                    system_prompt,
                    payload_messages,
                    tool_schemas,
                    text_callback=text_callback,
                    should_interrupt=should_interrupt,
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
                if callable(stream_flush_callback):
                    stream_flush_callback()
                session.latest_turn_id = uuid.uuid4().hex[:8]
                if not turn.has_tool_calls():
                    assistant_message = turn.as_message()
                    session.messages.append(assistant_message)
                    self.transcript_store.append(session.id, assistant_message)
                    final_text = "\n\n".join(turn.text_blocks).strip()
                    self._capture_turn_file_changes(session)
                    self.session_manager.save(session)
                    self._hook_manager().on_assistant_response(
                        session,
                        actor="lead",
                        trace_id=f"{session.id}-{session.latest_turn_id}",
                        assistant_message=assistant_message,
                        text=final_text,
                        execution_mode=getattr(self, "execution_mode", DEFAULT_EXECUTION_MODE),
                    )
                    return final_text

                tool_results: list[dict[str, Any]] = []
                executed_tool_calls = []
                used_todo = False
                manual_compact = False
                end_turn_after_tool = False
                for tool_call in turn.tool_calls:
                    self._raise_if_interrupted(should_interrupt)
                    ctx = ToolExecutionContext(
                        runtime=self,
                        session=session,
                        actor="lead",
                        trace_id=f"{session.id}-{session.latest_turn_id}",
                    )
                    if tool_call.name == "compress":
                        manual_compact = True
                    try:
                        output = self.registry.execute(ctx, tool_call.name, tool_call.input)
                    except Exception as exc:
                        output = f"Error: {exc}"
                    log_id = self.print_tool_event("lead", tool_call.name, tool_call.input, output)
                    executed_tool_calls.append(tool_call)
                    result = {
                        "type": "tool_result",
                        "tool_call_id": tool_call.id,
                        "content": str(output)[: self.settings.runtime.max_tool_output_chars],
                        "raw_output": output,
                        "log_id": log_id,
                    }
                    tool_results.append(result)
                    self.transcript_store.append(
                        session.id,
                        {
                            "role": "tool",
                            "name": tool_call.name,
                            "input": tool_call.input,
                            "output": result["content"],
                        },
                    )
                    if tool_call.name == "TodoWrite":
                        used_todo = True
                    if tool_call.name in self.TURN_BOUNDARY_TOOL_NAMES:
                        end_turn_after_tool = True
                        break

                assistant_message = turn.as_message(executed_tool_calls)
                session.messages.append(assistant_message)
                self.transcript_store.append(session.id, assistant_message)
                session.rounds_without_todo = 0 if used_todo else session.rounds_without_todo + 1
                if self.todo_manager.has_open_items(session) and session.rounds_without_todo >= 3:
                    tool_results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})
                session.messages.append(make_tool_result_message(tool_results))
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
                if end_turn_after_tool:
                    continue
            self._capture_turn_file_changes(session)
            self.session_manager.save(session)
            return final_text or "Stopped after max rounds."
        except TurnInterrupted:
            self.interrupt_active_teammates(reason="lead_interrupt")
            session.pending_file_changes = []
            session.last_turn_file_changes = []
            self.session_manager.save(session)
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
