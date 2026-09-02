from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from open_somnia import __version__
from open_somnia.cli.scripting import (
    EXIT_CONFIG_ERROR,
    EXIT_INTERNAL_ERROR,
    EXIT_USAGE_ERROR,
    CliError,
    emit_error_json,
    error_code_for_kind,
    exit_code_for_error_kind,
)
from open_somnia.config.settings import (
    ConfigParseError,
    NoConfiguredProvidersError,
    NoUsableProvidersError,
    global_config_path,
    load_settings,
    persist_initial_provider_setup,
    persist_provider_profile,
)
from open_somnia.providers.base import ProviderError
from open_somnia.runtime.agent import OpenAgentRuntime


class SomniaArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that exits with the documented usage-error code (64)."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE_ERROR, f"{self.prog}: error: {message}\n")


_COMMAND_FLAG_ALIASES = {
    "-trace": "trace",
    "-traceviewer": "trace-viewer",
}

_GLOBAL_OPTIONS_WITH_VALUES = {
    "--workspace",
    "--provider",
    "--model",
}


def choose_provider_target_interactively(existing_profiles):
    from open_somnia.cli.provider_management import choose_provider_target_interactively as choose_provider_target

    return choose_provider_target(existing_profiles)


def collect_provider_profile_interactively(existing_profiles, *, previous_provider_name: str | None = None):
    from open_somnia.cli.provider_management import collect_provider_profile_interactively as collect_provider_profile

    return collect_provider_profile(existing_profiles, previous_provider_name=previous_provider_name)


def _add_provider_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        default=argparse.SUPPRESS,
        help="Override the configured provider for this invocation.",
    )
    parser.add_argument(
        "--model",
        default=argparse.SUPPRESS,
        help="Override the configured model for this invocation.",
    )


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON on stdout (and structured JSON errors on stderr).",
    )


def _add_session_scope_flags(parser: argparse.ArgumentParser, *, default: str) -> None:
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--global",
        dest="scope",
        action="store_const",
        const="global",
        default=default,
        help="Use the global config file (~/.open_somnia/open_somnia.toml).",
    )
    scope_group.add_argument(
        "--project",
        dest="scope",
        action="store_const",
        const="project",
        help="Use the workspace config file (.open_somnia/open_somnia.toml).",
    )


def _normalize_command_aliases(argv: list[str] | None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    command_seen = False
    expecting_value = False

    for index, token in enumerate(args):
        if expecting_value:
            expecting_value = False
            continue
        if token in _GLOBAL_OPTIONS_WITH_VALUES:
            expecting_value = True
            continue
        if any(token.startswith(f"{option}=") for option in _GLOBAL_OPTIONS_WITH_VALUES):
            continue
        if not command_seen and token in _COMMAND_FLAG_ALIASES:
            args[index] = _COMMAND_FLAG_ALIASES[token]
            command_seen = True
            continue
        if token.startswith("-"):
            continue
        command_seen = True

    return args


def build_parser() -> argparse.ArgumentParser:
    parser = SomniaArgumentParser(prog="somnia")
    parser.add_argument(
        "-version",
        "--version",
        action="version",
        version=f"somnia {__version__}",
        help="Show the installed somnia version and exit.",
    )
    parser.add_argument("--workspace", default=".", help="Workspace root for the agent.")
    parser.add_argument(
        "-help",
        dest="help_cmd",
        nargs="?",
        const="",
        default=None,
        metavar="TOPIC",
        help=(
            "Show the somnia intro and all available commands, or detailed help "
            "for one command (e.g. -help run). Add --json for machine-readable output."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit help output as JSON (machine readable).",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "-r",
        "-resume",
        "--resume",
        dest="resume",
        action="store_true",
        help="Open the interactive session picker and resume a saved chat.",
    )
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the latest saved chat in this workspace.",
    )
    session_group.add_argument(
        "--session",
        dest="session_id",
        default=None,
        metavar="ID",
        help="Resume the saved session with this ID (skips the interactive picker).",
    )
    _add_provider_overrides(parser)
    subparsers = parser.add_subparsers(dest="command", parser_class=SomniaArgumentParser)

    chat_parser = subparsers.add_parser("chat", help="Start interactive chat mode.")
    chat_session_group = chat_parser.add_mutually_exclusive_group()
    chat_session_group.add_argument(
        "-r",
        "-resume",
        "--resume",
        dest="resume",
        action="store_true",
        help="Open the interactive session picker and resume a saved chat.",
    )
    chat_session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the latest saved chat in this workspace.",
    )
    chat_session_group.add_argument(
        "--session",
        dest="session_id",
        default=None,
        metavar="ID",
        help="Resume the saved session with this ID (skips the interactive picker).",
    )
    _add_provider_overrides(chat_parser)

    run_parser = subparsers.add_parser("run", help="Run a single prompt and exit.")
    run_parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Prompt to execute. May be omitted when -f/--file or piped stdin supplies the prompt.",
    )
    run_parser.add_argument(
        "-f",
        "--file",
        dest="file",
        default=None,
        metavar="PATH",
        help="Read prompt text from this file (combined with the prompt argument and piped stdin).",
    )
    run_session_group = run_parser.add_mutually_exclusive_group()
    run_session_group.add_argument(
        "--session",
        dest="session_id",
        default=None,
        metavar="ID",
        help="Continue the saved session with this ID (skips the interactive picker).",
    )
    run_session_group.add_argument(
        "--continue-last",
        dest="continue_last",
        action="store_true",
        help="Continue the latest saved session in this workspace.",
    )
    _add_json_flag(run_parser)
    run_parser.add_argument(
        "--plain",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Plain output: no ANSI styling and no bullet prefix (ideal for pipes).",
    )
    _add_provider_overrides(run_parser)

    sessions_parser = subparsers.add_parser("sessions", help="Inspect saved sessions non-interactively.")
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_command", required=True, parser_class=SomniaArgumentParser)
    sessions_list_parser = sessions_subparsers.add_parser("list", help="List saved sessions.")
    _add_json_flag(sessions_list_parser)
    sessions_fork_parser = sessions_subparsers.add_parser("fork", help="Fork a session, keeping its first N messages.")
    sessions_fork_parser.add_argument("session_id", help="Session id to fork from.")
    sessions_fork_parser.add_argument(
        "--at",
        type=int,
        required=True,
        help="Number of leading messages the fork keeps (fork point).",
    )
    _add_json_flag(sessions_fork_parser)

    config_parser = subparsers.add_parser("config", help="Read or modify configuration non-interactively.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True, parser_class=SomniaArgumentParser)
    config_get_parser = config_subparsers.add_parser("get", help="Get a config value by dotted key (e.g. providers.default).")
    config_get_parser.add_argument("key", help="Dotted config key to read.")
    _add_session_scope_flags(config_get_parser, default="merged")
    _add_json_flag(config_get_parser)
    config_set_parser = config_subparsers.add_parser("set", help="Set a config value by dotted key (default scope: --global).")
    config_set_parser.add_argument("key", help="Dotted config key to write (e.g. agent.name).")
    config_set_parser.add_argument(
        "value",
        help="Value as a TOML literal (true, 42, [\"a\", \"b\"]) or plain string.",
    )
    _add_session_scope_flags(config_set_parser, default="global")
    _add_json_flag(config_set_parser)

    capabilities_parser = subparsers.add_parser("capabilities", help="List available tools, models, and MCP servers.")
    _add_json_flag(capabilities_parser)
    _add_provider_overrides(capabilities_parser)

    tasks_parser = subparsers.add_parser("tasks", help="Inspect persistent tasks.")
    _add_provider_overrides(tasks_parser)
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command", required=True, parser_class=SomniaArgumentParser)
    tasks_subparsers.add_parser("list", help="List tasks.")
    get_parser = tasks_subparsers.add_parser("get", help="Get a task by ID.")
    get_parser.add_argument("task_id", type=int)

    compact_parser = subparsers.add_parser("compact", help="Compact the latest session.")
    _add_provider_overrides(compact_parser)
    doctor_parser = subparsers.add_parser("doctor", help="Validate runtime configuration.")
    _add_json_flag(doctor_parser)
    _add_provider_overrides(doctor_parser)
    trace_start_parser = subparsers.add_parser(
        "trace",
        help="Start Somnia with provider payload debug tracing enabled.",
    )
    trace_start_parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Optional prompt to run once. Omit to start interactive chat mode.",
    )
    trace_start_session_group = trace_start_parser.add_mutually_exclusive_group()
    trace_start_session_group.add_argument(
        "-r",
        "-resume",
        "--resume",
        dest="resume",
        action="store_true",
        help="Open the interactive session picker and resume a saved chat.",
    )
    trace_start_session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the latest saved chat in this workspace.",
    )
    trace_start_session_group.add_argument(
        "--session",
        dest="session_id",
        default=None,
        metavar="ID",
        help="Resume the saved session with this ID (skips the interactive picker).",
    )
    _add_provider_overrides(trace_start_parser)
    trace_parser = subparsers.add_parser(
        "traceviewer",
        aliases=["trace-viewer"],
        help="Generate an HTML viewer for provider payload debug dumps.",
    )
    trace_parser.set_defaults(command="trace-viewer")
    trace_parser.add_argument(
        "--session",
        dest="session_id",
        default=None,
        help="Only include provider payloads for this session ID.",
    )
    trace_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only include the latest N matching provider payloads.",
    )
    trace_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the HTML report to this path instead of the default provider payload log directory.",
    )
    providers_parser = subparsers.add_parser("providers", help="Add or edit shared provider profiles.")
    providers_subparsers = providers_parser.add_subparsers(dest="providers_command", parser_class=SomniaArgumentParser)
    providers_list_parser = providers_subparsers.add_parser("list", help="List configured provider profiles.")
    _add_json_flag(providers_list_parser)
    help_parser = subparsers.add_parser(
        "help",
        help="Show the somnia intro and all commands, or detailed help for one (alias: -help).",
    )
    help_parser.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Command to show detailed help for (e.g. run, tasks, /rollback).",
    )
    return parser


def _can_prompt_interactively() -> bool:
    stdin = getattr(sys.stdin, "isatty", None)
    stdout = getattr(sys.stdout, "isatty", None)
    return bool(callable(stdin) and stdin() and callable(stdout) and stdout())


def _parse_model_ids(raw_value: str) -> list[str]:
    models: list[str] = []
    for chunk in raw_value.split(","):
        model = chunk.strip()
        if model and model not in models:
            models.append(model)
    return models


def _default_base_url(provider_type: str) -> str:
    if provider_type == "openai":
        return "https://api.openai.com/v1"
    return "https://api.anthropic.com"


def _bootstrap_first_provider() -> bool:
    submission = collect_provider_profile_interactively({})
    if submission is None:
        return False
    persist_initial_provider_setup(
        submission.provider_name,
        submission.provider_type,
        submission.models,
        api_key=submission.api_key,
        base_url=submission.base_url,
    )
    return True


def _manage_providers(workspace: str) -> int:
    if not _can_prompt_interactively():
        raise CliError(
            f"Provider management is interactive. Edit {global_config_path()} manually, "
            "use 'somnia providers list --json' / 'somnia config set', or run this command in a TTY.",
            code="usage_error",
            exit_code=EXIT_USAGE_ERROR,
        )
    try:
        settings = load_settings(workspace)
        profiles = settings.provider_profiles
    except (NoConfiguredProvidersError, NoUsableProvidersError):
        profiles = {}

    selected = choose_provider_target_interactively(profiles)
    if not selected:
        print("Provider setup cancelled.", file=sys.stderr)
        return 1
    submission = collect_provider_profile_interactively(
        profiles,
        previous_provider_name=None if selected == "__add__" else selected,
    )
    if submission is None:
        print("Provider setup cancelled.", file=sys.stderr)
        return 1
    path = persist_provider_profile(
        submission.provider_name,
        submission.provider_type,
        submission.models,
        api_key=submission.api_key,
        base_url=submission.base_url,
        previous_provider_name=submission.previous_provider_name,
    )
    print(f"Saved provider '{submission.provider_name}' to {path}.")
    return 0


def _open_trace_report(settings) -> int:
    from open_somnia.cli.commands import cmd_trace_viewer

    print("Opening trace report...")
    return cmd_trace_viewer(settings, open_browser=True)


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "help" or getattr(args, "help_cmd", None) is not None:
        from open_somnia.cli.help import cli_help

        if getattr(args, "command", None) == "help":
            topic = getattr(args, "topic", None)
        else:
            topic = args.help_cmd or None
        return cli_help(topic, as_json=bool(getattr(args, "json", False)))
    if args.command == "trace" and args.prompt == "viewer":
        parser.error("Use 'somnia traceviewer' to open the trace report.")
    if args.command == "providers" and getattr(args, "providers_command", None) is None:
        return _manage_providers(args.workspace)

    as_json = bool(getattr(args, "json", False))

    # Commands that only need config/storage state: no provider bootstrap, no
    # runtime construction (which would eagerly connect MCP servers).
    if args.command in {"sessions", "providers", "config"}:
        settings = load_settings(
            args.workspace,
            provider_override=getattr(args, "provider", None),
            model_override=getattr(args, "model", None),
            allow_missing_provider=True,
        )
        from open_somnia.cli.commands import (
            cmd_config_get,
            cmd_config_set,
            cmd_providers_list,
            cmd_sessions_fork,
            cmd_sessions_list,
        )

        if args.command == "sessions":
            if args.sessions_command == "fork":
                return cmd_sessions_fork(settings, args.session_id, at=args.at, as_json=as_json)
            return cmd_sessions_list(settings, as_json=as_json)
        if args.command == "providers":
            return cmd_providers_list(settings, as_json=as_json)
        if args.config_command == "get":
            return cmd_config_get(settings, args.key, scope=args.scope, as_json=as_json)
        return cmd_config_set(settings, args.key, args.value, scope=args.scope, as_json=as_json)

    if args.command == "trace-viewer":
        settings = load_settings(
            args.workspace,
            provider_override=getattr(args, "provider", None),
            model_override=getattr(args, "model", None),
            allow_missing_provider=True,
        )
        from open_somnia.cli.commands import cmd_trace_viewer

        return cmd_trace_viewer(
            settings,
            session_id=args.session_id,
            limit=args.limit,
            output_path=args.output,
            open_browser=True,
        )

    try:
        settings = load_settings(
            args.workspace,
            provider_override=getattr(args, "provider", None),
            model_override=getattr(args, "model", None),
        )
    except NoConfiguredProvidersError as exc:
        if not _can_prompt_interactively():
            raise CliError(
                f"{exc}\nCreate your first provider in {global_config_path()} and run the command again.",
                code="config_error",
                exit_code=EXIT_CONFIG_ERROR,
            ) from exc
        if not _bootstrap_first_provider():
            raise CliError("Provider setup cancelled.", code="config_error", exit_code=EXIT_CONFIG_ERROR)
        settings = load_settings(
            args.workspace,
            provider_override=getattr(args, "provider", None),
            model_override=getattr(args, "model", None),
        )
    if args.command == "trace":
        os.environ[OpenAgentRuntime.DEBUG_PROVIDER_PAYLOAD_ENV] = "1"
    runtime = OpenAgentRuntime(settings)
    try:
        from open_somnia.cli.commands import (
            cmd_capabilities,
            cmd_chat,
            cmd_compact,
            cmd_doctor,
            cmd_run,
            cmd_tasks_get,
            cmd_tasks_list,
        )

        if args.command in {None, "chat"}:
            return cmd_chat(
                runtime,
                resume=getattr(args, "resume", False),
                continue_session=getattr(args, "continue_session", False),
                session_id=getattr(args, "session_id", None),
            )
        if args.command == "run":
            return cmd_run(
                runtime,
                args.prompt,
                file_path=getattr(args, "file", None),
                session_id=getattr(args, "session_id", None),
                continue_last=getattr(args, "continue_last", False),
                as_json=as_json,
                plain=bool(getattr(args, "plain", False)),
            )
        if args.command == "trace":
            provider = getattr(settings, "provider", None)
            provider_name = getattr(provider, "name", "unknown")
            provider_model = getattr(provider, "model", "unknown")
            provider_type = getattr(provider, "provider_type", "unknown")
            print(
                "Provider debug tracing enabled: "
                f"{provider_name} / {provider_model} ({provider_type})"
            )
            print(f"Trace payloads: {settings.storage.logs_dir / 'provider_payloads'}")
            print("Trace report will open automatically after exit.")
            if args.prompt is not None:
                status = cmd_run(runtime, args.prompt)
                if status == 0:
                    return _open_trace_report(settings)
                return status
            status = cmd_chat(
                runtime,
                resume=getattr(args, "resume", False),
                continue_session=getattr(args, "continue_session", False),
                session_id=getattr(args, "session_id", None),
            )
            if status == 0:
                return _open_trace_report(settings)
            return status
        if args.command == "tasks" and args.tasks_command == "list":
            return cmd_tasks_list(runtime)
        if args.command == "tasks" and args.tasks_command == "get":
            return cmd_tasks_get(runtime, args.task_id)
        if args.command == "compact":
            return cmd_compact(runtime)
        if args.command == "doctor":
            return cmd_doctor(runtime, as_json=as_json)
        if args.command == "capabilities":
            return cmd_capabilities(runtime, as_json=as_json)
        parser.error("Unsupported command")
    finally:
        runtime.close()
    return 1


def _report_error(
    code: str,
    message: str,
    *,
    exit_code: int,
    as_json: bool,
    exception_type: str | None = None,
) -> int:
    if as_json:
        extra = {"exception_type": exception_type} if exception_type else {}
        emit_error_json(code, message, **extra)
    else:
        print(f"Error: {message}", file=sys.stderr)
        if os.environ.get("SOMNIA_DEBUG"):
            traceback.print_exc()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_command_aliases(argv))
    as_json = bool(getattr(args, "json", False))
    try:
        return _dispatch(args, parser)
    except CliError as exc:
        return _report_error(exc.code, str(exc), exit_code=exc.exit_code, as_json=as_json)
    except ProviderError as exc:
        kind = getattr(exc, "kind", None)
        return _report_error(
            error_code_for_kind(kind),
            str(exc),
            exit_code=exit_code_for_error_kind(kind),
            as_json=as_json,
            exception_type=type(exc).__name__,
        )
    except (NoConfiguredProvidersError, ConfigParseError) as exc:
        return _report_error(
            "config_error",
            str(exc),
            exit_code=EXIT_CONFIG_ERROR,
            as_json=as_json,
            exception_type=type(exc).__name__,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if os.environ.get("SOMNIA_DEBUG"):
            raise
        return _report_error(
            "internal_error",
            str(exc) or type(exc).__name__,
            exit_code=EXIT_INTERNAL_ERROR,
            as_json=as_json,
            exception_type=type(exc).__name__,
        )


if __name__ == "__main__":
    raise SystemExit(main())
