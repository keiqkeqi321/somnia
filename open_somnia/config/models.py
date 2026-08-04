from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ModelTraits:
    context_window_tokens: int | None = None
    max_tokens: int | None = None
    reasoning_level: str | None = None
    supports_reasoning: bool | None = None
    supports_adaptive_reasoning: bool | None = None


@dataclass(slots=True)
class ProviderSettings:
    name: str = "anthropic"
    provider_type: str = "anthropic"
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    organization: str | None = None
    context_window_tokens: int | None = None
    max_tokens: int = 8_000
    timeout_seconds: int = 120
    reasoning_level: str | None = None
    supports_reasoning: bool | None = None
    supports_adaptive_reasoning: bool | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None


@dataclass(slots=True)
class ProviderProfileSettings:
    name: str
    provider_type: str = "anthropic"
    models: list[str] = field(default_factory=list)
    model_traits: dict[str, ModelTraits] = field(default_factory=dict)
    default_model: str = ""
    api_key: str = ""
    base_url: str | None = None
    organization: str | None = None
    context_window_tokens: int | None = None
    max_tokens: int = 8_000
    timeout_seconds: int = 120
    reasoning_level: str | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None


@dataclass(slots=True)
class MCPServerSettings:
    name: str
    transport: str = "stdio"
    url: str | None = None
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    http_headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_seconds: int = 30
    startup_timeout_seconds: int = 30
    protocol_version: str = "2025-11-25"
    # Per-tool subsetting. None = key absent (all tools enabled by default).
    # An explicitly empty include_tools list means "no tools enabled" — the
    # distinction matters when UIs toggle tools one by one. When both are set,
    # exclude wins.
    include_tools: list[str] | None = None
    exclude_tools: list[str] | None = None

    def tool_enabled(self, tool_name: str) -> bool:
        if self.include_tools is not None:
            if tool_name not in self.include_tools:
                return False
            if self.exclude_tools and tool_name in self.exclude_tools:
                return False
            return True
        if self.exclude_tools:
            return tool_name not in self.exclude_tools
        return True


@dataclass(slots=True)
class HookMatcherSettings:
    tool_name: str | None = None
    actor: str | None = None


@dataclass(slots=True)
class HookSettings:
    event: str
    command: str
    args: list[str] = field(default_factory=list)
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 10
    on_error: str = "continue"
    enabled: bool = True
    background: bool = False
    managed_by: str | None = None
    config_path: Path | None = None
    config_scope: str | None = None
    config_index: int | None = None
    matcher: HookMatcherSettings = field(default_factory=HookMatcherSettings)


@dataclass(slots=True)
class RuntimeSettings:
    janitor_trigger_ratio: float = 0.6
    command_timeout_seconds: int = 120
    background_poll_interval_seconds: int = 2
    teammate_idle_timeout_seconds: int = 60
    teammate_poll_interval_seconds: int = 5
    max_tool_output_chars: int = 50_000
    max_tool_calls_per_turn: int = 64
    exploration_soft_limit: int = 10
    exploration_hard_streak_limit: int = 14
    exploration_hard_total_limit: int = 0
    max_subagent_rounds: int = 30
    max_agent_rounds: int = 100


@dataclass(slots=True)
class AgentSettings:
    name: str = "Somnia"
    system_prompt: str | None = None


@dataclass(slots=True)
class StorageSettings:
    data_dir: Path
    transcripts_dir: Path
    sessions_dir: Path
    tasks_dir: Path
    inbox_dir: Path
    team_dir: Path
    jobs_dir: Path
    requests_dir: Path
    logs_dir: Path
    state_dir: Path


@dataclass(slots=True)
class AppSettings:
    workspace_root: Path
    agent: AgentSettings
    provider: ProviderSettings
    runtime: RuntimeSettings
    storage: StorageSettings
    vision_provider: str | None = None
    vision_model: str | None = None
    provider_profiles: dict[str, ProviderProfileSettings] = field(default_factory=dict)
    mcp_servers: list[MCPServerSettings] = field(default_factory=list)
    hooks: list[HookSettings] = field(default_factory=list)
    raw_config: dict[str, Any] = field(default_factory=dict)
    config_recovery_message: str | None = None
