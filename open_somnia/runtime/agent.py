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
import uuid
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

from open_somnia.collaboration.bus import MessageBus
from open_somnia.collaboration.protocols import RequestTracker
from open_somnia.config.models import AppSettings, ProviderProfileSettings, ProviderSettings
from open_somnia.config.settings import _materialize_provider, load_settings, persist_provider_selection
from open_somnia.mcp.registry import MCPRegistry
from open_somnia.providers.anthropic_provider import AnthropicProvider
from open_somnia.providers.base import LLMProvider, ProviderError
from open_somnia.providers.openai_provider import OpenAIProvider
from open_somnia.runtime.compact import (
    CompactManager,
    ContextWindowUsage,
    build_payload_messages,
    estimate_payload_tokens,
    should_auto_compact,
)
from open_somnia.runtime.execution_mode import (
    AUTHORIZATION_TOOL_NAME,
    DEFAULT_EXECUTION_MODE,
    MODE_SWITCH_TOOL_NAME,
    NON_YOLO_EXECUTION_MODES,
)
from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import make_tool_result_message, make_user_text_message
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
from open_somnia.storage.common import now_ts
from open_somnia.storage.repo_summary import RepoSummaryStore
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
    EXPLORATION_CACHE_LIMIT = 10
    EXPLORATION_PROMPT_CHAR_LIMIT = 2_000
    REPO_SUMMARY_PROMPT_CHAR_LIMIT = 2_500
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
        self.repo_summary_store = RepoSummaryStore(settings.storage.data_dir)
        self.task_store = TaskStore(settings.storage.tasks_dir)
        self.job_store = JobStore(settings.storage.jobs_dir)
        self.tool_log_store = ToolLogStore(settings.storage.logs_dir)
        self.inbox_store = InboxStore(settings.storage.inbox_dir)
        self.bus = MessageBus(self.inbox_store)
        self.team_store = TeamStore(settings.storage.team_dir)
        self.request_tracker = RequestTracker(settings.storage.requests_dir)
        self.skill_loader = SkillLoader.for_workspace(settings.workspace_root)
        self.todo_manager = TodoManager()
        self.background_manager = BackgroundManager(
            self.job_store,
            settings.workspace_root,
            settings.runtime.command_timeout_seconds,
            settings.runtime.max_tool_output_chars,
        )
        self.compact_manager = CompactManager(self.provider, self.transcript_store, settings.provider.max_tokens)
        self._context_usage_cache: dict[str, tuple[tuple[Any, ...], ContextWindowUsage]] = {}
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

    def _messages_for_model(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return build_payload_messages(messages)

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
        payload_messages = self._messages_for_model(messages)
        try:
            system_prompt = self.build_system_prompt(actor=actor, role=role, session=session)
        except TypeError:
            system_prompt = self.build_system_prompt()
        tools = self._context_usage_tools(actor)
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
            return cached[1]

        provider = getattr(self, "provider", None)
        counter_name = "estimate"
        try:
            if provider is not None and callable(getattr(provider, "count_tokens", None)):
                used_tokens = int(provider.count_tokens(system_prompt, payload_messages, tools))
                if used_tokens <= 0 and (system_prompt.strip() or payload_messages or tools):
                    raise ValueError("Provider token counter returned a non-positive token count for a non-empty payload.")
                counter_name = str(provider.token_counter_name())
            else:
                raise RuntimeError("Provider token counting unavailable.")
        except Exception:
            used_tokens = estimate_payload_tokens(system_prompt, payload_messages, tools)

        context_window_tokens = None
        if provider is not None and callable(getattr(provider, "context_window_tokens", None)):
            context_window_tokens = provider.context_window_tokens()
        if context_window_tokens is None:
            context_window_tokens = getattr(getattr(self.settings, "provider", None), "context_window_tokens", None)

        usage = ContextWindowUsage(
            used_tokens=used_tokens,
            max_tokens=int(context_window_tokens) if context_window_tokens is not None else None,
            counter_name=counter_name,
        )
        cache[session.id] = (cache_key, usage)
        return usage

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
        return self.session_manager.create()

    def latest_session(self) -> AgentSession:
        return self.session_manager.latest_or_create()

    def load_session(self, session_id: str) -> AgentSession:
        return self.session_manager.load(session_id)

    def list_sessions(self) -> list[AgentSession]:
        return self.session_manager.list_all()

    def _ensure_exploration_cache(self, session: AgentSession) -> dict[str, Any]:
        cache = getattr(session, "exploration_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            session.exploration_cache = cache
        return cache

    def _push_exploration_entry(self, session: AgentSession, key: str, entry: dict[str, Any]) -> None:
        cache = self._ensure_exploration_cache(session)
        items = list(cache.get(key, []))
        items.insert(0, entry)
        cache[key] = items[: self.EXPLORATION_CACHE_LIMIT]

    def record_project_scan(self, session: AgentSession, *, path: str, summary_text: str, source: str = "command") -> None:
        timestamp = now_ts()
        entry = {
            "path": path,
            "summary_text": summary_text,
            "source": source,
            "updated_at": timestamp,
        }
        cache = self._ensure_exploration_cache(session)
        cache["last_project_scan"] = entry
        self._push_exploration_entry(session, "project_scans", entry)
        self.repo_summary_store.update_scan(path=path, summary_text=summary_text)
        self.session_manager.save(session)

    def record_symbol_lookup(
        self,
        session: AgentSession,
        *,
        query: str,
        path: str,
        kind: str,
        matches: list[dict[str, Any]],
        source: str = "command",
    ) -> None:
        timestamp = now_ts()
        entry = {
            "query": query,
            "path": path,
            "kind": kind,
            "match_count": len(matches),
            "matches": matches[:10],
            "source": source,
            "updated_at": timestamp,
        }
        cache = self._ensure_exploration_cache(session)
        cache["last_symbol_lookup"] = entry
        self._push_exploration_entry(session, "symbol_queries", entry)
        self.repo_summary_store.record_symbol_query(
            query=query,
            path=path,
            kind=kind,
            match_count=len(matches),
        )
        self.session_manager.save(session)

    def cached_project_scan(self, session: AgentSession, *, path: str) -> dict[str, Any] | None:
        cache = self._ensure_exploration_cache(session)
        for item in list(cache.get("project_scans", [])):
            if str(item.get("path", "")).strip() == path:
                return item
        return None

    def repo_summary_prompt(self) -> str:
        store = getattr(self, "repo_summary_store", None)
        if store is None:
            return ""
        try:
            payload = store.load()
        except Exception:
            return ""
        summary_text = str(payload.get("summary_text", "")).strip()
        if not summary_text:
            return ""
        if len(summary_text) > self.REPO_SUMMARY_PROMPT_CHAR_LIMIT:
            summary_text = summary_text[: self.REPO_SUMMARY_PROMPT_CHAR_LIMIT].rstrip() + "\n..."
        return summary_text

    def session_exploration_prompt(self, session: AgentSession | None) -> str:
        if session is None:
            return ""
        cache = getattr(session, "exploration_cache", None)
        if not isinstance(cache, dict):
            return ""
        lines: list[str] = []
        last_scan = cache.get("last_project_scan")
        if isinstance(last_scan, dict):
            path = str(last_scan.get("path", ".")).strip() or "."
            summary_text = str(last_scan.get("summary_text", "")).strip()
            if summary_text:
                lines.append(f"Last /scan path: {path}")
                lines.append(summary_text)
        recent_symbols = list(cache.get("symbol_queries", []))[:3]
        if recent_symbols:
            if lines:
                lines.append("")
            lines.append("Recent symbol lookups:")
            for item in recent_symbols:
                query = str(item.get("query", "")).strip()
                path = str(item.get("path", ".")).strip() or "."
                kind = str(item.get("kind", "")).strip() or "any"
                count = int(item.get("match_count", 0))
                lines.append(f"- {query} | path={path} | kind={kind} | matches={count}")
        text = "\n".join(line for line in lines if line is not None).strip()
        if len(text) > self.EXPLORATION_PROMPT_CHAR_LIMIT:
            text = text[: self.EXPLORATION_PROMPT_CHAR_LIMIT].rstrip() + "\n..."
        return text

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
        self.session_manager.save(session)

    def _raise_if_interrupted(self, should_interrupt) -> None:
        if should_interrupt is not None and should_interrupt():
            raise TurnInterrupted("Interrupted by user.")

    def run_turn(self, session: AgentSession, user_input: str, text_callback=None, should_interrupt=None) -> str:
        session.pending_file_changes = []
        session.last_turn_file_changes = []
        session.messages.append(make_user_text_message(user_input))
        self.transcript_store.append(session.id, {"role": "user", "content": user_input})
        return self._agent_loop(session, text_callback=text_callback, should_interrupt=should_interrupt)

    def _agent_loop(self, session: AgentSession, text_callback=None, should_interrupt=None) -> str:
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
                    session.messages = self.compact_manager.auto_compact(session.id, session.messages)

                stream_flush_callback = getattr(text_callback, "finish", None) if text_callback is not None else None
                payload_messages = self._messages_for_model(session.messages)

                try:
                    system_prompt = self.build_system_prompt(session=session)
                except TypeError:
                    system_prompt = self.build_system_prompt()

                turn = self.complete(
                    system_prompt,
                    payload_messages,
                    self.registry.schemas(),
                    text_callback=text_callback,
                    should_interrupt=should_interrupt,
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
                    session.messages = self.compact_manager.auto_compact(session.id, session.messages)
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
