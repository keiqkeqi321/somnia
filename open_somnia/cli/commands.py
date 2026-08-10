from __future__ import annotations

import json
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from open_somnia.app_service import AppService
from open_somnia.app_service.events import (
    ASSISTANT_DELTA,
    AUTHORIZATION_REQUESTED,
    MODE_SWITCH_REQUESTED,
    TOOL_FINISHED,
)
from open_somnia.app_service.models import TurnRunResult
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.messages import MarkdownStreamRenderer, render_markdown_text, render_message_content, render_text_content
from open_somnia.cli.prompting import choose_session_interactively, format_session_timestamp
from open_somnia.cli.scripting import (
    EXIT_CONFIG_ERROR,
    EXIT_SESSION_NOT_FOUND,
    EXIT_USAGE_ERROR,
    CliError,
    emit_json,
    error_code_for_kind,
    exit_code_for_error_kind,
)
from open_somnia.analysis.trace_viewer import build_trace_viewer_report, provider_payload_dir


ASSISTANT_BULLET = "\u25cf"
USER_BULLET = "\u276f"


def _supports_output_text(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return False
    except LookupError:
        return True
    return True


def _prefix_first_line(text: str, prefix: str) -> str:
    if not text:
        return prefix.rstrip()
    lines = text.splitlines()
    if not lines:
        return prefix.rstrip()
    lines[0] = f"{prefix}{lines[0]}"
    return "\n".join(lines)


def _assistant_prefix(*, ansi: bool) -> str:
    if ansi:
        return "\x1b[37m\u25cf\x1b[0m "
    bullet = ASSISTANT_BULLET if _supports_output_text(ASSISTANT_BULLET) else "*"
    return f"{bullet} "


def _user_prefix(*, ansi: bool) -> str:
    if ansi:
        return "\x1b[38;5;45m\u276f\x1b[0m "
    bullet = USER_BULLET if _supports_output_text(USER_BULLET) else ">"
    return f"{bullet} "


def print_user_message(text: str, *, ansi: bool | None = None) -> None:
    ansi_enabled = sys.stdout.isatty() if ansi is None else ansi
    lines = text.splitlines() or [""]
    first = f"{_user_prefix(ansi=ansi_enabled)}{lines[0]}"
    remainder = [f"  {line}" if line else "  " for line in lines[1:]]
    print()
    print(first)
    for line in remainder:
        print(line)
    print()


class ConsoleStreamer:
    def __init__(
        self,
        start_on_new_line: bool = False,
        line_buffered: bool = False,
        on_first_output=None,
        *,
        ansi: bool | None = None,
        show_prefix: bool = True,
    ) -> None:
        self.has_output = False
        self.start_on_new_line = start_on_new_line
        self.line_buffered = line_buffered
        self.on_first_output = on_first_output
        self._ansi = sys.stdout.isatty() if ansi is None else ansi
        self._show_prefix = show_prefix
        self._renderer: MarkdownStreamRenderer | None = None
        self._started_printing = False

    def __call__(self, text: str) -> None:
        if not text:
            return
        if self._renderer is None:
            self._renderer = MarkdownStreamRenderer(ansi=self._ansi)
        if not self.has_output and self.on_first_output is not None:
            self.on_first_output()
        self._print_rendered(self._renderer.feed(text))
        self.has_output = True

    def finish(self) -> None:
        if not self.has_output:
            return
        if self._renderer is None:
            return
        self._print_rendered(self._renderer.finish())

    def _print_rendered(self, rendered: str) -> None:
        if not rendered:
            return
        if self.start_on_new_line and not self._started_printing:
            print()
        if not self._started_printing and self._show_prefix:
            rendered = _prefix_first_line(rendered, _assistant_prefix(ansi=self._ansi))
        print(rendered, end="" if rendered.endswith("\n") else "\n", flush=True)
        self._started_printing = True


@dataclass(slots=True)
class SessionChoice:
    session_id: str
    label: str


def _has_visible_exchange(session) -> bool:
    has_user = False
    has_assistant = False
    for message in session.messages:
        role = message.get("role")
        content = message.get("content")
        if role == "user" and isinstance(content, str):
            if content.startswith("<background-results>") or content.startswith("<inbox>"):
                continue
            if content.strip():
                has_user = True
        elif role == "assistant":
            text = render_text_content(content).strip()
            if text:
                has_assistant = True
        if has_user and has_assistant:
            return True
    return False


def _session_preview(session) -> str:
    for message in reversed(session.messages):
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            text = render_message_content(content, ansi=False).strip()
        elif role == "user" and isinstance(content, str):
            if content.startswith("<background-results>") or content.startswith("<inbox>"):
                continue
            text = content.strip()
        else:
            continue
        if text:
            return " ".join(text.split())[:80]
    return "[no visible messages]"


def _build_session_choices(runtime) -> list[SessionChoice]:
    choices: list[SessionChoice] = []
    list_summaries = getattr(runtime, "list_session_summaries", None)
    if callable(list_summaries):
        for summary in list_summaries():
            if not summary.get("has_visible_exchange"):
                continue
            session_id = str(summary.get("id", "")).strip()
            if not session_id:
                continue
            stamp = format_session_timestamp(summary.get("updated_at") or summary.get("created_at"))
            preview = str(summary.get("preview") or "[no visible messages]")
            label = f"{session_id} | {stamp} | {preview}"
            choices.append(SessionChoice(session_id=session_id, label=label))
        return choices
    for session in runtime.list_sessions():
        if not _has_visible_exchange(session):
            continue
        stamp = format_session_timestamp(session.updated_at or session.created_at)
        preview = _session_preview(session)
        label = f"{session.id} | {stamp} | {preview}"
        choices.append(SessionChoice(session_id=session.id, label=label))
    return choices


def _select_session(runtime):
    choices = _build_session_choices(runtime)
    if not choices:
        print("No saved sessions. Starting a new chat.")
        return runtime.create_session(), False

    selected_id = choose_session_interactively([(item.session_id, item.label) for item in choices])
    if not selected_id:
        print("Session selection cancelled. Starting a new chat.")
        return runtime.create_session(), False
    return runtime.load_session(selected_id), True


def _select_latest_session(runtime):
    choices = _build_session_choices(runtime)
    if not choices:
        print("No saved sessions. Starting a new chat.")
        return runtime.create_session(), False
    return runtime.load_session(choices[0].session_id), True


def _build_app_service(runtime) -> AppService | None:
    if isinstance(runtime, OpenAgentRuntime):
        return AppService(runtime)
    return None


def _print_service_tool_event(payload: dict[str, Any]) -> None:
    tool_name = str(payload.get("tool_name", "")).strip()
    actor = str(payload.get("actor", "")).strip() or "lead"
    if tool_name == "TodoWrite" or actor != "lead" or not sys.stdout.isatty():
        return
    rendered_lines = payload.get("rendered_lines")
    if not isinstance(rendered_lines, list) or not rendered_lines:
        return
    print()
    for line in rendered_lines:
        print(str(line))
    print()


_TOKEN_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

_STOPPED_STATUSES = {"stopped_with_open_todos", "stopped_after_max_rounds", "stopped_empty_response"}


def _token_usage_snapshot(session) -> dict[str, int]:
    usage = getattr(session, "token_usage", None)
    if not isinstance(usage, dict):
        return {key: 0 for key in _TOKEN_USAGE_KEYS}
    return {key: int(usage.get(key, 0) or 0) for key in _TOKEN_USAGE_KEYS}


def _run_result_envelope(
    runtime: OpenAgentRuntime,
    session,
    result: TurnRunResult,
    usage_before: dict[str, int],
    duration_ms: int,
) -> dict[str, Any]:
    usage_after = _token_usage_snapshot(session)
    provider = getattr(session, "provider_override", None) or getattr(runtime.settings.provider, "name", None)
    model = getattr(session, "model_override", None) or getattr(runtime.settings.provider, "model", None)
    return {
        "session_id": getattr(session, "id", None),
        "status": result.status,
        "text": result.text,
        "usage": {key: usage_after[key] - usage_before.get(key, 0) for key in _TOKEN_USAGE_KEYS},
        "provider": provider,
        "model": model,
        "duration_ms": duration_ms,
    }


def _run_service_turn_to_console(
    runtime: OpenAgentRuntime,
    service: AppService,
    session,
    prompt: str,
    *,
    as_json: bool = False,
    plain: bool = False,
) -> TurnRunResult:
    streamer = ConsoleStreamer(ansi=False if plain else None, show_prefix=not plain)
    usage_before = _token_usage_snapshot(session)
    started = time.perf_counter()
    handle = service.run_turn(session, prompt)

    while True:
        batch = handle.drain_events(block=not handle.is_done(), timeout=0.05)
        if batch:
            for event in batch:
                payload = getattr(event, "payload", {}) or {}
                if event.type == ASSISTANT_DELTA:
                    if not as_json:
                        streamer(str(payload.get("delta", "")))
                elif event.type == TOOL_FINISHED:
                    if not as_json:
                        streamer.finish()
                        _print_service_tool_event(payload)
                elif event.type == AUTHORIZATION_REQUESTED:
                    request_id = str(payload.get("request_id", "")).strip()
                    if request_id:
                        service.resolve_authorization(
                            request_id,
                            scope="deny",
                            approved=False,
                            reason="Interactive approvals are unavailable in this session.",
                        )
                elif event.type == MODE_SWITCH_REQUESTED:
                    request_id = str(payload.get("request_id", "")).strip()
                    if request_id:
                        service.resolve_mode_switch(
                            request_id,
                            approved=False,
                            active_mode=getattr(runtime, "execution_mode", None),
                            reason="Interactive mode switching is unavailable in this session.",
                        )
            continue
        if handle.is_done():
            trailing = handle.drain_events()
            if trailing:
                batch = trailing
                for event in batch:
                    payload = getattr(event, "payload", {}) or {}
                    if event.type == ASSISTANT_DELTA:
                        if not as_json:
                            streamer(str(payload.get("delta", "")))
                    elif event.type == TOOL_FINISHED:
                        if not as_json:
                            streamer.finish()
                            _print_service_tool_event(payload)
                continue
            break

    result = handle.result or TurnRunResult(session=session, text="", status="failed", error="Turn failed.")
    result_status = str(getattr(result, "status", "")).strip()
    if result_status == "failed":
        error_kind = getattr(result, "error_kind", None)
        raise CliError(
            str(getattr(result, "error", "")).strip() or "Turn failed.",
            code=error_code_for_kind(error_kind),
            exit_code=exit_code_for_error_kind(error_kind),
        )
    duration_ms = int((time.perf_counter() - started) * 1000)
    if as_json:
        streamer.finish()
        emit_json(_run_result_envelope(runtime, session, result, usage_before, duration_ms))
        return result
    if streamer.has_output:
        streamer.finish()
        if result_status in _STOPPED_STATUSES and result.text and sys.stdout.isatty() and not plain:
            print()
            print(_prefix_first_line(render_markdown_text(result.text, ansi=True), _assistant_prefix(ansi=True)))
    elif result.text:
        if plain:
            print(result.text)
        else:
            print(_prefix_first_line(render_markdown_text(result.text, ansi=sys.stdout.isatty()), _assistant_prefix(ansi=sys.stdout.isatty())))
    return result


def _read_piped_stdin() -> str:
    stdin = sys.stdin
    try:
        if stdin is None or stdin.isatty():
            return ""
    except Exception:
        return ""
    return stdin.read()


def _collect_run_prompt(prompt: str | None, *, file_path: str | Path | None = None) -> str:
    parts: list[str] = []
    if prompt and prompt.strip():
        parts.append(prompt)
    if file_path:
        path = Path(file_path)
        try:
            file_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(
                f"Cannot read prompt file '{path}': {exc}",
                code="usage_error",
                exit_code=EXIT_USAGE_ERROR,
            ) from exc
        if file_text.strip():
            parts.append(file_text)
    stdin_text = _read_piped_stdin()
    if stdin_text.strip():
        parts.append(stdin_text)
    combined = "\n\n".join(parts).strip()
    if not combined:
        raise CliError(
            "No prompt provided. Pass a prompt argument, -f/--file, or pipe one on stdin.",
            code="usage_error",
            exit_code=EXIT_USAGE_ERROR,
        )
    return combined


def _latest_session_or_new(service):
    choices = _build_session_choices(service)
    if not choices:
        return service.create_session()
    return service.load_session(choices[0].session_id)


def _resolve_run_session(service, *, session_id: str | None, continue_last: bool):
    if session_id:
        try:
            return service.load_session(session_id)
        except ValueError:
            raise CliError(
                f"Unknown session '{session_id}'.",
                code="session_not_found",
                exit_code=EXIT_SESSION_NOT_FOUND,
            ) from None
    if continue_last:
        return _latest_session_or_new(service)
    return service.create_session()


def cmd_chat(
    runtime: OpenAgentRuntime,
    resume: bool = False,
    continue_session: bool = False,
    session_id: str | None = None,
) -> int:
    from open_somnia.cli.repl import run_repl

    service = _build_app_service(runtime)
    session_api = service or runtime
    if session_id:
        try:
            session, resumed = session_api.load_session(session_id), True
        except ValueError:
            raise CliError(
                f"Unknown session '{session_id}'.",
                code="session_not_found",
                exit_code=EXIT_SESSION_NOT_FOUND,
            ) from None
    elif resume:
        session, resumed = _select_session(session_api)
    elif continue_session:
        session, resumed = _select_latest_session(session_api)
    else:
        session, resumed = session_api.create_session(), False
    return run_repl(runtime, session, resumed=resumed, service=service)


def cmd_run(
    runtime: OpenAgentRuntime,
    prompt: str | None,
    *,
    file_path: str | Path | None = None,
    session_id: str | None = None,
    continue_last: bool = False,
    as_json: bool = False,
    plain: bool = False,
) -> int:
    prompt_text = _collect_run_prompt(prompt, file_path=file_path)
    service = _build_app_service(runtime)
    if service is not None:
        session = _resolve_run_session(service, session_id=session_id, continue_last=continue_last)
        _run_service_turn_to_console(runtime, service, session, prompt_text, as_json=as_json, plain=plain)
        return 0

    session = runtime.create_session()
    streamer = ConsoleStreamer(ansi=False if plain else None, show_prefix=not plain)
    result = runtime.run_turn(session, prompt_text, text_callback=streamer)
    result_status = str(getattr(result, "status", "")).strip()
    if as_json:
        emit_json({"session_id": getattr(session, "id", None), "status": result_status or "completed", "text": str(result)})
        return 0
    if streamer.has_output:
        streamer.finish()
        if result_status in _STOPPED_STATUSES and result and sys.stdout.isatty() and not plain:
            print()
            print(_prefix_first_line(render_markdown_text(result, ansi=True), _assistant_prefix(ansi=True)))
    elif result:
        if plain:
            print(str(result))
        else:
            print(_prefix_first_line(render_markdown_text(result, ansi=sys.stdout.isatty()), _assistant_prefix(ansi=sys.stdout.isatty())))
    return 0


def cmd_tasks_list(runtime: OpenAgentRuntime) -> int:
    tasks = runtime.task_store.list_all()
    if not tasks:
        print("No tasks.")
    else:
        for task in tasks:
            print(json.dumps(task, ensure_ascii=False, indent=2))
    return 0


def cmd_tasks_get(runtime: OpenAgentRuntime, task_id: int) -> int:
    print(json.dumps(runtime.task_store.get(task_id), ensure_ascii=False, indent=2))
    return 0


def cmd_compact(runtime: OpenAgentRuntime) -> int:
    session = runtime.latest_session()
    runtime.compact_session(session)
    print(f"Compacted session {session.id}")
    return 0


def cmd_sessions_list(settings, *, as_json: bool = False) -> int:
    from open_somnia.storage.sessions import SessionStore

    store = SessionStore(settings.storage.sessions_dir)
    summaries = store.list_summaries()
    if as_json:
        emit_json({"sessions": summaries})
        return 0
    if not summaries:
        print("No saved sessions.")
        return 0
    for summary in summaries:
        stamp = format_session_timestamp(summary.get("updated_at") or summary.get("created_at"))
        preview = str(summary.get("preview") or "[no visible messages]")
        print(f"{summary.get('id')} | {stamp} | {preview}")
    return 0


def _provider_profile_payload(name: str, profile, *, default_name: str, active_name: str) -> dict[str, Any]:
    return {
        "name": name,
        "provider_type": str(getattr(profile, "provider_type", "") or ""),
        "models": list(getattr(profile, "models", []) or []),
        "default_model": str(getattr(profile, "default_model", "") or ""),
        "base_url": getattr(profile, "base_url", None),
        "api_key_configured": bool(str(getattr(profile, "api_key", "") or "").strip()),
        "is_default": name == default_name,
        "is_active": name == active_name,
    }


def cmd_providers_list(settings, *, as_json: bool = False) -> int:
    profiles = getattr(settings, "provider_profiles", {}) or {}
    raw_providers = (getattr(settings, "raw_config", {}) or {}).get("providers", {})
    default_name = str(raw_providers.get("default", "") or "").strip().lower() if isinstance(raw_providers, dict) else ""
    if not default_name and profiles:
        default_name = next(iter(profiles))
    active_name = str(getattr(getattr(settings, "provider", None), "name", "") or "").strip().lower()

    payloads = [
        _provider_profile_payload(name, profile, default_name=default_name, active_name=active_name)
        for name, profile in profiles.items()
    ]
    if as_json:
        emit_json({"providers": payloads})
        return 0
    if not payloads:
        print("No providers configured.")
        return 0
    for payload in payloads:
        markers = []
        if payload["is_default"]:
            markers.append("default")
        if payload["is_active"]:
            markers.append("active")
        marker_text = f" [{', '.join(markers)}]" if markers else ""
        api_key_text = "yes" if payload["api_key_configured"] else "no"
        print(
            f"{payload['name']} ({payload['provider_type']}){marker_text} "
            f"default_model={payload['default_model'] or '-'} models={len(payload['models'])} api_key={api_key_text}"
        )
    return 0


def _config_raw_for_scope(settings, scope: str) -> dict[str, Any]:
    from open_somnia.config import settings as settings_module

    if scope == "global":
        return settings_module._read_toml(settings_module.global_config_path())
    if scope == "project":
        return settings_module._read_toml(settings_module.workspace_config_path(settings.workspace_root))
    return dict(getattr(settings, "raw_config", {}) or {})


def _print_config_value(value: Any) -> None:
    if isinstance(value, bool):
        print("true" if value else "false")
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def cmd_config_get(settings, key: str, *, scope: str = "merged", as_json: bool = False) -> int:
    from open_somnia.config.settings import get_config_value

    raw = _config_raw_for_scope(settings, scope)
    try:
        value = get_config_value(raw, key)
    except KeyError:
        raise CliError(
            f"Config key '{key}' not found in {scope} config.",
            code="config_error",
            exit_code=EXIT_CONFIG_ERROR,
        ) from None
    if as_json:
        emit_json({"key": key, "scope": scope, "value": value})
    else:
        _print_config_value(value)
    return 0


def cmd_config_set(settings, key: str, value: str, *, scope: str = "global", as_json: bool = False) -> int:
    from open_somnia.config import settings as settings_module

    if scope == "project":
        config_path = settings_module.workspace_config_path(settings.workspace_root)
    else:
        config_path = settings_module.global_config_path()
    try:
        written_path = settings_module.set_config_value(config_path, key, value)
    except ValueError as exc:
        raise CliError(str(exc), code="usage_error", exit_code=EXIT_USAGE_ERROR) from exc
    if as_json:
        emit_json({"key": key, "value": value, "path": str(written_path)})
    else:
        print(f"Set {key} in {written_path}")
    return 0


def cmd_capabilities(runtime: OpenAgentRuntime, *, as_json: bool = False) -> int:
    from open_somnia import __version__

    settings = runtime.settings
    tools = [
        {"name": schema.get("name", ""), "description": schema.get("description", "")}
        for schema in runtime.registry.schemas()
    ]
    mcp_servers = runtime.mcp_registry.server_summaries() if getattr(runtime, "mcp_registry", None) else []
    payload = {
        "version": __version__,
        "provider": {
            "name": settings.provider.name,
            "type": settings.provider.provider_type,
            "model": settings.provider.model,
        },
        "configured_providers": sorted(getattr(settings, "provider_profiles", {}) or {}),
        "tools": tools,
        "mcp_servers": mcp_servers,
    }
    if as_json:
        emit_json(payload)
        return 0
    print(f"version: {payload['version']}")
    print(f"provider: {settings.provider.name} ({settings.provider.provider_type}) model={settings.provider.model}")
    print(f"configured_providers: {', '.join(payload['configured_providers']) or '-'}")
    print(f"tools ({len(tools)}): {', '.join(tool['name'] for tool in tools)}")
    if mcp_servers:
        print("mcp_servers:")
        for server in mcp_servers:
            print(f"  {server['name']}: {server['status']} [{server['transport']}] tools={server['tool_count']}")
    else:
        print("mcp_servers: none configured")
    return 0


def cmd_doctor(runtime: OpenAgentRuntime, *, as_json: bool = False) -> int:
    settings = runtime.settings
    api_key_configured = bool(str(getattr(settings.provider, "api_key", "") or "").strip())
    if as_json:
        emit_json(
            {
                "workspace": str(settings.workspace_root),
                "provider": settings.provider.name,
                "model": settings.provider.model,
                "api_key_configured": api_key_configured,
                "configured_providers": sorted(getattr(settings, "provider_profiles", {}) or {}),
                "skills_dir_present": (settings.workspace_root / "skills").exists(),
                "data_dir": str(settings.storage.data_dir),
                "state_dir": str(settings.storage.state_dir),
                "mcp_servers": runtime.mcp_registry.server_summaries() if settings.mcp_servers else [],
            }
        )
    else:
        print(runtime.doctor())
    if not settings.provider.name or not api_key_configured:
        return EXIT_CONFIG_ERROR
    return 0


def cmd_trace_viewer(
    settings,
    *,
    session_id: str | None = None,
    limit: int | None = None,
    output_path: Path | None = None,
    open_browser: bool = False,
) -> int:
    logs_dir = settings.storage.logs_dir
    report_path = build_trace_viewer_report(
        logs_dir,
        output_path=output_path,
        session_id=session_id,
        limit=limit,
    )
    payload_dir = provider_payload_dir(logs_dir)
    print(f"Generated trace viewer: {report_path}")
    print(f"Provider payload source: {payload_dir}")
    if open_browser:
        webbrowser.open(report_path.resolve().as_uri())
    return 0
