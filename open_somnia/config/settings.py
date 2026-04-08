from __future__ import annotations

import tomllib
from pathlib import Path

from open_somnia.config.models import (
    AgentSettings,
    AppSettings,
    MCPServerSettings,
    ModelTraits,
    ProviderProfileSettings,
    ProviderSettings,
    RuntimeSettings,
    StorageSettings,
)

APP_DIRNAME = ".open_somnia"
CONFIG_FILENAME = "open_somnia.toml"
DEFAULT_AGENT_NAME = "Somnia"


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _merge_config(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_config(existing, value)
        else:
            merged[key] = value
    return merged


def global_config_path() -> Path:
    return Path.home() / APP_DIRNAME / CONFIG_FILENAME


def workspace_config_path(workspace_root: Path) -> Path:
    return workspace_root / APP_DIRNAME / CONFIG_FILENAME


def load_raw_config(workspace_root: Path) -> dict:
    global_raw = _read_toml(global_config_path())
    workspace_raw = _read_toml(workspace_config_path(workspace_root))
    return _merge_config(global_raw, workspace_raw)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _section_header(name: str) -> str:
    return f"[{name}]"


def _find_section_bounds(lines: list[str], section_name: str) -> tuple[int | None, int | None]:
    header = _section_header(section_name)
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == header:
            start = index
            continue
        if start is not None and stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    if start is not None and end is None:
        end = len(lines)
    return start, end


def _upsert_section_value(lines: list[str], section_name: str, key: str, value: str) -> None:
    start, end = _find_section_bounds(lines, section_name)
    assignment = f"{key} = {value}"
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(_section_header(section_name))
        lines.append(assignment)
        return

    assert end is not None
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith(f"{key} ="):
            lines[index] = assignment
            return
    insert_at = start + 1
    while insert_at < end and not lines[insert_at].strip():
        insert_at += 1
    lines.insert(insert_at, assignment)


def persist_provider_selection(settings: AppSettings, provider_name: str, model: str) -> None:
    config_path = workspace_config_path(settings.workspace_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.exists() else []
    _upsert_section_value(lines, "providers", "default", _toml_string(provider_name))
    _upsert_section_value(lines, f"providers.{provider_name}", "default_model", _toml_string(model))
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    providers_raw = settings.raw_config.setdefault("providers", {})
    if not isinstance(providers_raw, dict):
        providers_raw = {}
        settings.raw_config["providers"] = providers_raw
    providers_raw["default"] = provider_name
    provider_raw = providers_raw.setdefault(provider_name, {})
    if not isinstance(provider_raw, dict):
        provider_raw = {}
        providers_raw[provider_name] = provider_raw
    provider_raw["default_model"] = model


def _resolve_optional_path(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def _build_mcp_server(root: Path, name: str, item: dict) -> MCPServerSettings:
    transport = str(item.get("transport", "http" if item.get("url") else "stdio")).lower()
    return MCPServerSettings(
        name=name,
        transport=transport,
        url=str(item["url"]) if item.get("url") else None,
        command=str(item.get("command", "")),
        args=[str(arg) for arg in item.get("args", [])],
        cwd=_resolve_optional_path(root, item.get("cwd")),
        env={str(k): str(v) for k, v in item.get("env", {}).items()},
        http_headers={str(k): str(v) for k, v in item.get("http_headers", {}).items()},
        enabled=bool(item.get("enabled", True)),
        timeout_seconds=int(item.get("timeout_seconds", item.get("request_timeout_sec", 30))),
        startup_timeout_seconds=int(item.get("startup_timeout_sec", item.get("timeout_seconds", 30))),
        protocol_version=str(item.get("protocol_version", "2025-11-25")),
    )


def _load_mcp_servers(root: Path, raw: dict) -> list[MCPServerSettings]:
    mcp_servers: list[MCPServerSettings] = []
    servers_raw = raw.get("mcp_servers", {})
    if isinstance(servers_raw, list):
        for item in servers_raw:
            if not isinstance(item, dict) or "name" not in item:
                continue
            mcp_servers.append(_build_mcp_server(root, str(item["name"]), item))
        return mcp_servers
    if isinstance(servers_raw, dict):
        for name, item in servers_raw.items():
            if not isinstance(item, dict):
                continue
            mcp_servers.append(_build_mcp_server(root, str(name), item))
    return mcp_servers


def _storage_settings(workspace_root: Path) -> StorageSettings:
    data_dir = workspace_root / APP_DIRNAME
    return StorageSettings(
        data_dir=data_dir,
        transcripts_dir=data_dir / "transcripts",
        sessions_dir=data_dir / "sessions",
        tasks_dir=data_dir / "tasks",
        inbox_dir=data_dir / "inbox",
        team_dir=data_dir / "team",
        jobs_dir=data_dir / "jobs",
        requests_dir=data_dir / "requests",
        logs_dir=data_dir / "logs",
    )


def _default_provider_type(name: str) -> str:
    provider_name = name.strip().lower()
    if provider_name == "openai":
        return "openai"
    return "anthropic"


def _normalize_provider_type(value: str | None, *, profile_name: str) -> str:
    provider_type = str(value or "").strip().lower() or _default_provider_type(profile_name)
    if provider_type not in {"anthropic", "openai"}:
        raise ValueError(
            f"Provider '{profile_name}' has unsupported provider_type '{provider_type}'. "
            "Expected 'anthropic' or 'openai'."
        )
    return provider_type


def _infer_context_window_tokens(provider_type: str, model: str) -> int:
    lowered = model.strip().lower()
    if provider_type == "anthropic" or "claude" in lowered:
        return 200_000
    if "gpt-4.1" in lowered:
        return 1_047_576
    if any(token in lowered for token in ("gpt-5", "o1", "o3", "o4", "gpt-4o")):
        return 128_000
    if any(token in lowered for token in ("qwen", "glm", "kimi", "deepseek", "llama", "mistral", "gemini")):
        return 128_000
    return 128_000


def _default_provider_profile(name: str) -> ProviderProfileSettings:
    provider_name = name.strip().lower()
    provider_type = _default_provider_type(provider_name)
    if provider_type == "openai":
        return ProviderProfileSettings(
            name=provider_name,
            provider_type=provider_type,
            models=["gpt-4.1"],
            model_traits={},
            default_model="gpt-4.1",
            api_key="",
            base_url="https://api.openai.com/v1",
            organization=None,
            context_window_tokens=None,
        )
    return ProviderProfileSettings(
        name=provider_name,
        provider_type=provider_type,
        models=["claude-sonnet-4-5"],
        model_traits={},
        default_model="claude-sonnet-4-5",
        api_key="",
        base_url=None,
        context_window_tokens=None,
    )


def _build_model_traits(item: dict) -> ModelTraits:
    context_window_tokens = item.get("cwt", item.get("context_window_tokens"))
    return ModelTraits(
        context_window_tokens=int(context_window_tokens) if context_window_tokens is not None else None,
    )


def _is_model_traits_leaf(item: object) -> bool:
    if not isinstance(item, dict) or not item:
        return False
    return any(not isinstance(value, dict) for value in item.values())


def _load_global_model_traits(raw: dict) -> dict[str, ModelTraits]:
    traits_root = raw.get("model_traits", {})
    if not isinstance(traits_root, dict):
        return {}
    model_traits: dict[str, ModelTraits] = {}
    for model_name, item in traits_root.items():
        if not _is_model_traits_leaf(item):
            continue
        normalized_model_name = str(model_name).strip()
        if not normalized_model_name:
            continue
        model_traits[normalized_model_name] = _build_model_traits(item)
    return model_traits


def _load_provider_model_traits(raw: dict, provider_name: str) -> dict[str, ModelTraits]:
    traits_root = raw.get("model_traits", {})
    if not isinstance(traits_root, dict):
        return {}
    provider_traits_raw = {}
    for raw_provider_name, item in traits_root.items():
        if str(raw_provider_name).strip().lower() == provider_name:
            provider_traits_raw = item
            break
    if not isinstance(provider_traits_raw, dict):
        return {}
    model_traits: dict[str, ModelTraits] = {}
    for model_name, item in provider_traits_raw.items():
        if not _is_model_traits_leaf(item):
            continue
        normalized_model_name = str(model_name).strip()
        if not normalized_model_name:
            continue
        model_traits[normalized_model_name] = _build_model_traits(item)
    return model_traits


def _build_provider_profile(name: str, item: dict, raw: dict) -> ProviderProfileSettings:
    provider_name = name.strip().lower()
    defaults = _default_provider_profile(provider_name)
    provider_type = _normalize_provider_type(item.get("provider_type"), profile_name=provider_name)
    raw_models = item.get("models", [])
    models = [str(model).strip() for model in raw_models if str(model).strip()]
    default_model = str(item.get("default_model", "")).strip() or defaults.default_model
    if default_model and default_model not in models:
        models = [*models, default_model]
    if not models:
        models = list(defaults.models)
        default_model = defaults.default_model
    model_traits = dict(_load_global_model_traits(raw))
    model_traits.update(_load_provider_model_traits(raw, provider_name))
    return ProviderProfileSettings(
        name=provider_name,
        provider_type=provider_type,
        models=models,
        model_traits=model_traits,
        default_model=default_model or models[0],
        api_key=str(item.get("api_key", defaults.api_key)),
        base_url=str(item["base_url"]) if item.get("base_url") else defaults.base_url,
        organization=str(item["organization"]) if item.get("organization") else defaults.organization,
        context_window_tokens=int(item["context_window_tokens"])
        if item.get("context_window_tokens") is not None
        else defaults.context_window_tokens,
        max_tokens=int(item.get("max_tokens", defaults.max_tokens)),
        timeout_seconds=int(item.get("timeout_seconds", defaults.timeout_seconds)),
    )


def _load_provider_profiles(raw: dict) -> tuple[dict[str, ProviderProfileSettings], str]:
    providers_raw = raw.get("providers", {})
    profiles: dict[str, ProviderProfileSettings] = {}
    configured_default = ""
    if isinstance(providers_raw, dict):
        configured_default = str(providers_raw.get("default", "")).strip().lower()
        for name, item in providers_raw.items():
            if name == "default" or not isinstance(item, dict):
                continue
            profiles[str(name).strip().lower()] = _build_provider_profile(str(name), item, raw)
    if not profiles:
        fallback_name = "anthropic"
        profiles[fallback_name] = _default_provider_profile(fallback_name)
        return profiles, fallback_name
    if configured_default:
        if configured_default not in profiles:
            raise ValueError(f"Configured default provider '{configured_default}' is not defined in [providers].")
        return profiles, configured_default
    return profiles, next(iter(profiles))


def _materialize_provider(profile: ProviderProfileSettings, model: str | None = None) -> ProviderSettings:
    selected_model = (model or profile.default_model).strip()
    if selected_model not in profile.models:
        raise ValueError(f"Model '{selected_model}' is not configured for provider '{profile.name}'.")
    model_traits = profile.model_traits.get(selected_model)
    return ProviderSettings(
        name=profile.name,
        provider_type=profile.provider_type,
        model=selected_model,
        api_key=profile.api_key,
        base_url=profile.base_url,
        organization=profile.organization,
        context_window_tokens=(
            model_traits.context_window_tokens
            if model_traits and model_traits.context_window_tokens is not None
            else profile.context_window_tokens or _infer_context_window_tokens(profile.provider_type, selected_model)
        ),
        max_tokens=profile.max_tokens,
        timeout_seconds=profile.timeout_seconds,
    )


def load_settings(
    workspace_root: str | Path | None = None,
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> AppSettings:
    root = Path(workspace_root or Path.cwd()).resolve()
    raw = load_raw_config(root)
    agent_raw = raw.get("agent", {})
    agent = AgentSettings(
        name=str(agent_raw.get("name", DEFAULT_AGENT_NAME)).strip() or DEFAULT_AGENT_NAME,
        system_prompt=str(agent_raw["system_prompt"]).strip() if agent_raw.get("system_prompt") else None,
    )

    provider_profiles, configured_provider_name = _load_provider_profiles(raw)
    provider_name = (provider_override or configured_provider_name).strip().lower()
    if provider_name not in provider_profiles:
        raise ValueError(f"Provider '{provider_name}' is not configured in [providers].")
    provider = _materialize_provider(provider_profiles[provider_name], model_override)

    runtime_raw = raw.get("runtime", {})
    runtime = RuntimeSettings(
        token_threshold=int(runtime_raw.get("token_threshold", 100_000)),
        command_timeout_seconds=int(runtime_raw.get("command_timeout_seconds", 120)),
        background_poll_interval_seconds=int(runtime_raw.get("background_poll_interval_seconds", 2)),
        teammate_idle_timeout_seconds=int(runtime_raw.get("teammate_idle_timeout_seconds", 60)),
        teammate_poll_interval_seconds=int(runtime_raw.get("teammate_poll_interval_seconds", 5)),
        max_tool_output_chars=int(runtime_raw.get("max_tool_output_chars", 50_000)),
        max_subagent_rounds=int(runtime_raw.get("max_subagent_rounds", 30)),
        max_agent_rounds=int(runtime_raw.get("max_agent_rounds", 50)),
    )

    mcp_servers = _load_mcp_servers(root, raw)

    settings = AppSettings(
        workspace_root=root,
        agent=agent,
        provider=provider,
        runtime=runtime,
        storage=_storage_settings(root),
        provider_profiles=provider_profiles,
        mcp_servers=mcp_servers,
        raw_config=raw,
    )
    ensure_storage_dirs(settings)
    return settings


def ensure_storage_dirs(settings: AppSettings) -> None:
    for path in (
        settings.storage.data_dir,
        settings.storage.transcripts_dir,
        settings.storage.sessions_dir,
        settings.storage.tasks_dir,
        settings.storage.inbox_dir,
        settings.storage.team_dir,
        settings.storage.jobs_dir,
        settings.storage.requests_dir,
        settings.storage.logs_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
