from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from open_somnia.config.models import (
    AgentSettings,
    AppSettings,
    HookMatcherSettings,
    HookSettings,
    MCPServerSettings,
    ModelTraits,
    ProviderProfileSettings,
    ProviderSettings,
    RuntimeSettings,
    StorageSettings,
)
from open_somnia.hooks.models import normalize_hook_event
from open_somnia.storage.common import atomic_write_text

APP_DIRNAME = ".open_somnia"
CONFIG_FILENAME = "open_somnia.toml"
DEFAULT_AGENT_NAME = "Somnia"
HOOKS_DIRNAME = "Hooks"
BUILTIN_NOTIFY_FOLDER = "builtin_notify"
BUILTIN_NOTIFY_MANAGER = "somnia_builtin_notify"
BUILTIN_HOOKS_BEGIN = "# BEGIN SOMNIA BUILTIN HOOKS"
BUILTIN_HOOKS_END = "# END SOMNIA BUILTIN HOOKS"


class NoConfiguredProvidersError(RuntimeError):
    """Raised when neither global nor workspace config defines any providers."""


class NoUsableProvidersError(NoConfiguredProvidersError):
    """Raised when providers exist but none has an API key configured."""


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _merge_config(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if key == "hooks" and isinstance(existing, list) and isinstance(value, list):
            merged[key] = list(existing) + list(value)
            continue
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_config(existing, value)
        else:
            merged[key] = value
    return merged


def global_config_path() -> Path:
    return Path.home() / APP_DIRNAME / CONFIG_FILENAME


def global_hooks_root() -> Path:
    return Path.home() / APP_DIRNAME / HOOKS_DIRNAME


def workspace_config_path(workspace_root: Path) -> Path:
    return workspace_root / APP_DIRNAME / CONFIG_FILENAME


def load_raw_config(workspace_root: Path) -> dict:
    _ensure_global_builtin_notify_hooks()
    global_raw = _read_toml(global_config_path())
    workspace_raw = _read_toml(workspace_config_path(workspace_root))
    return _merge_config(global_raw, workspace_raw)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _section_header(name: str) -> str:
    return f"[{name}]"


def _section_name(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return None


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


def _remove_section(lines: list[str], section_name: str) -> list[str]:
    start, end = _find_section_bounds(lines, section_name)
    if start is None or end is None:
        return list(lines)
    updated = list(lines[:start]) + list(lines[end:])
    while updated and not updated[0].strip():
        updated.pop(0)
    while updated and not updated[-1].strip():
        updated.pop()
    normalized: list[str] = []
    previous_blank = False
    for line in updated:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized.append(line)
        previous_blank = is_blank
    return normalized


def _remove_provider_sections(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_section = False
    for line in lines:
        current_section = _section_name(line)
        if current_section is not None:
            skip_section = current_section == "providers" or current_section.startswith("providers.")
        if skip_section:
            continue
        cleaned.append(line)

    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    normalized: list[str] = []
    previous_blank = False
    for line in cleaned:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized.append(line)
        previous_blank = is_blank
    return normalized


def _clear_provider_config(path: Path) -> bool:
    if not path.exists():
        return False
    original_lines = path.read_text(encoding="utf-8").splitlines()
    cleaned_lines = _remove_provider_sections(original_lines)
    if cleaned_lines == original_lines:
        return False
    if cleaned_lines:
        path.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


def clear_stale_provider_config(workspace_root: Path) -> None:
    _clear_provider_config(global_config_path())
    _clear_provider_config(workspace_config_path(workspace_root))


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


def persist_initial_provider_setup(
    provider_name: str,
    provider_type: str,
    models: list[str],
    *,
    api_key: str,
    base_url: str,
) -> Path:
    return persist_provider_profile(
        provider_name,
        provider_type,
        models,
        api_key=api_key,
        base_url=base_url,
    )


def persist_provider_profile(
    provider_name: str,
    provider_type: str,
    models: list[str],
    *,
    api_key: str,
    base_url: str,
    previous_provider_name: str | None = None,
) -> Path:
    normalized_provider_name = str(provider_name).strip().lower()
    if not normalized_provider_name:
        raise ValueError("A provider name is required to configure the provider.")
    previous_name = str(previous_provider_name or "").strip().lower() or None
    normalized_provider_type = _normalize_provider_type(provider_type, profile_name=normalized_provider_name)
    normalized_models: list[str] = []
    for model in models:
        normalized_model = str(model).strip()
        if normalized_model and normalized_model not in normalized_models:
            normalized_models.append(normalized_model)
    if not normalized_models:
        raise ValueError("At least one model id is required to configure the provider.")
    normalized_api_key = str(api_key).strip()
    if not normalized_api_key:
        raise ValueError("An API key is required to configure the provider.")
    normalized_base_url = str(base_url).strip()
    if not normalized_base_url:
        raise ValueError("A base URL is required to configure the provider.")

    config_path = global_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.exists() else []
    raw = _read_toml(config_path)
    providers_raw = raw.get("providers", {}) if isinstance(raw.get("providers", {}), dict) else {}
    current_default_name = str(providers_raw.get("default", "")).strip().lower()
    existing_raw = providers_raw.get(previous_name or normalized_provider_name, {})
    existing_default_model = (
        str(existing_raw.get("default_model", "")).strip() if isinstance(existing_raw, dict) else ""
    )

    if previous_name and previous_name != normalized_provider_name:
        lines = _remove_section(lines, f"providers.{previous_name}")

    provider_section = f"providers.{normalized_provider_name}"
    should_update_default = not current_default_name or current_default_name in {
        previous_name or "",
        normalized_provider_name,
    }
    if should_update_default:
        _upsert_section_value(lines, "providers", "default", _toml_string(normalized_provider_name))
    _upsert_section_value(lines, provider_section, "provider_type", _toml_string(normalized_provider_type))
    _upsert_section_value(lines, provider_section, "models", _toml_array(normalized_models))
    default_model = existing_default_model if existing_default_model in normalized_models else normalized_models[0]
    _upsert_section_value(lines, provider_section, "default_model", _toml_string(default_model))
    _upsert_section_value(lines, provider_section, "api_key", _toml_string(normalized_api_key))
    _upsert_section_value(lines, provider_section, "base_url", _toml_string(normalized_base_url))
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _ensure_global_builtin_notify_hooks(config_path=config_path)
    return config_path


def _resolve_optional_path(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def _build_mcp_server(root: Path, name: str, item: dict) -> MCPServerSettings:
    transport = str(item.get("transport", "http" if item.get("url") else "stdio")).lower()
    url_value = str(item["url"]).strip() if item.get("url") else ""
    if transport == "http":
        resolved_url = url_value or None
    else:
        # Avoid stale merged HTTP URL when an override explicitly switches to stdio.
        resolved_url = None
    return MCPServerSettings(
        name=name,
        transport=transport,
        url=resolved_url,
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


def _build_hook(
    root: Path,
    item: dict,
    *,
    config_path: Path | None = None,
    config_scope: str | None = None,
    config_index: int | None = None,
) -> HookSettings:
    matcher_raw = item.get("matcher", {})
    if not isinstance(matcher_raw, dict):
        matcher_raw = {}
    return HookSettings(
        event=str(item.get("event", "")).strip(),
        command=str(item.get("command", "")).strip(),
        args=[str(arg) for arg in item.get("args", [])],
        cwd=_resolve_optional_path(root, item.get("cwd")),
        env={str(k): str(v) for k, v in item.get("env", {}).items()},
        timeout_seconds=int(item.get("timeout_seconds", 10)),
        on_error=str(item.get("on_error", "continue")).strip().lower() or "continue",
        enabled=bool(item.get("enabled", True)),
        managed_by=str(item.get("managed_by", "")).strip() or None,
        config_path=config_path.resolve() if isinstance(config_path, Path) else None,
        config_scope=str(config_scope).strip() or None,
        config_index=config_index,
        matcher=HookMatcherSettings(
            tool_name=str(matcher_raw.get("tool_name", "")).strip() or None,
            actor=str(matcher_raw.get("actor", "")).strip() or None,
        ),
    )


def _load_hooks(
    root: Path,
    raw: dict,
    *,
    config_path: Path | None = None,
    config_scope: str | None = None,
) -> list[HookSettings]:
    hooks_raw = raw.get("hooks", [])
    if not isinstance(hooks_raw, list):
        return []
    hooks: list[HookSettings] = []
    for index, item in enumerate(hooks_raw):
        if not isinstance(item, dict):
            continue
        if not str(item.get("event", "")).strip() or not str(item.get("command", "")).strip():
            continue
        hooks.append(
            _build_hook(
                root,
                item,
                config_path=config_path,
                config_scope=config_scope,
                config_index=index,
            )
        )
    return hooks


def _merge_hooks(global_hooks: list[HookSettings], workspace_hooks: list[HookSettings]) -> list[HookSettings]:
    workspace_override_keys = {
        (normalize_hook_event(hook.event), hook.managed_by)
        for hook in workspace_hooks
        if str(hook.managed_by or "").strip()
    }
    merged: list[HookSettings] = []
    for hook in global_hooks:
        key = (normalize_hook_event(hook.event), hook.managed_by)
        if key in workspace_override_keys:
            continue
        merged.append(hook)
    merged.extend(workspace_hooks)
    return merged


def _builtin_notify_source_path() -> Path:
    return Path(__file__).resolve().parent.parent / "hooks" / "notify_user.py"


def _builtin_notify_install_dir() -> Path:
    return global_hooks_root() / BUILTIN_NOTIFY_FOLDER


def _builtin_notify_target_script() -> Path:
    return _builtin_notify_install_dir() / "notify_user.py"


def _install_builtin_notify_assets() -> Path:
    source = _builtin_notify_source_path()
    target = _builtin_notify_target_script()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, source.read_text(encoding="utf-8"))
    return target


def _strip_builtin_hook_block(lines: list[str]) -> list[str]:
    stripped: list[str] = []
    in_block = False
    for line in lines:
        marker = line.strip()
        if marker == BUILTIN_HOOKS_BEGIN:
            in_block = True
            continue
        if marker == BUILTIN_HOOKS_END:
            in_block = False
            continue
        if not in_block:
            stripped.append(line)
    while stripped and not stripped[-1].strip():
        stripped.pop()
    return stripped


def _strip_managed_hook_entries(lines: list[str], *, managed_by: str) -> list[str]:
    stripped: list[str] = []
    block: list[str] = []
    in_hook_block = False

    def flush_hook_block() -> None:
        nonlocal block, in_hook_block
        if not block:
            return
        managed = any(line.strip() == f'managed_by = "{managed_by}"' for line in block)
        if not managed:
            stripped.extend(block)
        block = []
        in_hook_block = False

    for line in lines:
        marker = line.strip()
        if marker == "[[hooks]]":
            flush_hook_block()
            block = [line]
            in_hook_block = True
            continue
        if in_hook_block and marker.startswith("[") and marker.endswith("]") and marker != "[hooks.matcher]":
            flush_hook_block()
            stripped.append(line)
            continue
        if in_hook_block:
            block.append(line)
            continue
        stripped.append(line)
    flush_hook_block()
    while stripped and not stripped[-1].strip():
        stripped.pop()
    return stripped


def _render_builtin_hook_block(script_path: Path, hooks: list[dict[str, object]]) -> list[str]:
    block = [BUILTIN_HOOKS_BEGIN]
    command = str(Path(sys.executable).resolve())
    script_value = str(script_path.resolve())
    for hook in hooks:
        event = normalize_hook_event(str(hook.get("event", "")).strip())
        enabled = bool(hook.get("enabled", True))
        block.extend(
            [
                "[[hooks]]",
                f'event = {_toml_string(event)}',
                f'command = {_toml_string(command)}',
                f"args = {_toml_array([script_value])}",
                'managed_by = "somnia_builtin_notify"',
                "timeout_seconds = 10",
                'on_error = "continue"',
                f"enabled = {'true' if enabled else 'false'}",
                "",
            ]
        )
    if block[-1] == "":
        block.pop()
    block.append(BUILTIN_HOOKS_END)
    return block


def _builtin_notify_items(raw: dict) -> dict[str, dict[str, object]]:
    hooks_raw = raw.get("hooks", [])
    if not isinstance(hooks_raw, list):
        return {}
    items: dict[str, dict[str, object]] = {}
    for item in hooks_raw:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event", "")).strip()
        if not event:
            continue
        if str(item.get("managed_by", "")).strip() == BUILTIN_NOTIFY_MANAGER:
            normalized = normalize_hook_event(event)
            items[normalized] = {
                "event": normalized,
                "enabled": bool(item.get("enabled", True)),
            }
    return items


def _ensure_global_builtin_notify_hooks(*, config_path: Path | None = None) -> None:
    config_path = config_path or global_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.exists() else []
    base_lines = _strip_managed_hook_entries(_strip_builtin_hook_block(lines), managed_by=BUILTIN_NOTIFY_MANAGER)
    raw = _read_toml(config_path)
    existing_items = _builtin_notify_items(raw)
    target_script = _install_builtin_notify_assets()
    desired_hooks = [
        existing_items.get(event, {"event": event, "enabled": True})
        for event in ("AssistantResponse", "UserChoiceRequested")
    ]
    updated = list(base_lines)
    if updated and updated[-1].strip():
        updated.append("")
    updated.extend(_render_builtin_hook_block(target_script, desired_hooks))
    if updated == lines:
        return
    config_path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _find_hook_block_bounds(lines: list[str], hook_index: int) -> tuple[int | None, int | None]:
    current_index = -1
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[[hooks]]":
            current_index += 1
            if current_index == hook_index:
                start = index
                continue
            if start is not None:
                return start, index
        if start is not None and stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[hooks."):
            return start, index
    if start is None:
        return None, None
    return start, len(lines)


def _upsert_hook_boolean(lines: list[str], hook_index: int, key: str, value: bool) -> None:
    start, end = _find_hook_block_bounds(lines, hook_index)
    if start is None or end is None:
        raise ValueError(f"Hook entry #{hook_index} was not found in config.")
    assignment = f"{key} = {'true' if value else 'false'}"
    insert_at = end
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith(f"{key} ="):
            lines[index] = assignment
            return
        if stripped.startswith("[hooks."):
            insert_at = index
            break
    lines.insert(insert_at, assignment)


def persist_hook_enabled(hook: HookSettings, enabled: bool) -> Path:
    config_path = getattr(hook, "config_path", None)
    config_index = getattr(hook, "config_index", None)
    if not isinstance(config_path, Path) or config_index is None:
        raise ValueError("Hook origin metadata is missing; cannot persist enabled state.")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.exists() else []
    _upsert_hook_boolean(lines, int(config_index), "enabled", enabled)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


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
        raise NoConfiguredProvidersError(
            "No providers are configured. Add a provider to open_somnia.toml or complete first-run setup."
        )
    if configured_default:
        if configured_default not in profiles:
            raise ValueError(f"Configured default provider '{configured_default}' is not defined in [providers].")
        return profiles, configured_default
    return profiles, next(iter(profiles))


def _has_configured_api_key(profiles: dict[str, ProviderProfileSettings]) -> bool:
    return any(profile.api_key.strip() for profile in profiles.values())


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
    _ensure_global_builtin_notify_hooks()
    global_raw = _read_toml(global_config_path())
    workspace_raw = _read_toml(workspace_config_path(root))
    raw = _merge_config(global_raw, workspace_raw)
    agent_raw = raw.get("agent", {})
    agent = AgentSettings(
        name=str(agent_raw.get("name", DEFAULT_AGENT_NAME)).strip() or DEFAULT_AGENT_NAME,
        system_prompt=str(agent_raw["system_prompt"]).strip() if agent_raw.get("system_prompt") else None,
    )

    provider_profiles, configured_provider_name = _load_provider_profiles(raw)
    if not _has_configured_api_key(provider_profiles):
        clear_stale_provider_config(root)
        raise NoUsableProvidersError(
            "No providers with API keys are configured. Cleared stale provider configuration and need first-run setup."
        )
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
    hooks = _merge_hooks(
        _load_hooks(root, global_raw, config_path=global_config_path(), config_scope="global"),
        _load_hooks(root, workspace_raw, config_path=workspace_config_path(root), config_scope="workspace"),
    )

    settings = AppSettings(
        workspace_root=root,
        agent=agent,
        provider=provider,
        runtime=runtime,
        storage=_storage_settings(root),
        provider_profiles=provider_profiles,
        mcp_servers=mcp_servers,
        hooks=hooks,
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
