from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ModelTraits:
    context_window_tokens: int | None = None


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
    managed_by: str | None = None
    config_path: Path | None = None
    config_scope: str | None = None
    config_index: int | None = None
    matcher: HookMatcherSettings = field(default_factory=HookMatcherSettings)


@dataclass(slots=True)
class RuntimeSettings:
    token_threshold: int = 100_000
    command_timeout_seconds: int = 120
    background_poll_interval_seconds: int = 2
    teammate_idle_timeout_seconds: int = 60
    teammate_poll_interval_seconds: int = 5
    max_tool_output_chars: int = 50_000
    max_subagent_rounds: int = 30
    max_agent_rounds: int = 50


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


@dataclass(slots=True)
class AppSettings:
    workspace_root: Path
    agent: AgentSettings
    provider: ProviderSettings
    runtime: RuntimeSettings
    storage: StorageSettings
    provider_profiles: dict[str, ProviderProfileSettings] = field(default_factory=dict)
    mcp_servers: list[MCPServerSettings] = field(default_factory=list)
    hooks: list[HookSettings] = field(default_factory=list)
    raw_config: dict[str, Any] = field(default_factory=dict)
