from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from open_somnia import __version__
from open_somnia.config.settings import (
    NoConfiguredProvidersError,
    NoUsableProvidersError,
    global_config_path,
    load_settings,
    persist_initial_provider_setup,
    persist_provider_profile,
)
from open_somnia.runtime.agent import OpenAgentRuntime

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
    parser = argparse.ArgumentParser(prog="somnia")
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
    _add_provider_overrides(parser)
    subparsers = parser.add_subparsers(dest="command")

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
    _add_provider_overrides(chat_parser)

    run_parser = subparsers.add_parser("run", help="Run a single prompt.")
    run_parser.add_argument("prompt", help="Prompt to execute.")
    _add_provider_overrides(run_parser)

    tasks_parser = subparsers.add_parser("tasks", help="Inspect persistent tasks.")
    _add_provider_overrides(tasks_parser)
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command", required=True)
    tasks_subparsers.add_parser("list", help="List tasks.")
    get_parser = tasks_subparsers.add_parser("get", help="Get a task by ID.")
    get_parser.add_argument("task_id", type=int)

    compact_parser = subparsers.add_parser("compact", help="Compact the latest session.")
    _add_provider_overrides(compact_parser)
    doctor_parser = subparsers.add_parser("doctor", help="Validate runtime configuration.")
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
    subparsers.add_parser("providers", help="Add or edit shared provider profiles.")
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
        print(
            f"Provider management is interactive. Edit {global_config_path()} manually or run this command in a TTY.",
            file=sys.stderr,
        )
        return 2
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_command_aliases(argv))
    if args.command == "help" or getattr(args, "help_cmd", None) is not None:
        from open_somnia.cli.help import cli_help

        if getattr(args, "command", None) == "help":
            topic = getattr(args, "topic", None)
        else:
            topic = args.help_cmd or None
        return cli_help(topic, as_json=args.json)
    if args.command == "trace" and args.prompt == "viewer":
        parser.error("Use 'somnia traceviewer' to open the trace report.")
    if args.command == "providers":
        return _manage_providers(args.workspace)
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
            print(
                f"{exc}\nCreate your first provider in {global_config_path()} and run the command again.",
                file=sys.stderr,
            )
            return 2
        if not _bootstrap_first_provider():
            print("Provider setup cancelled.", file=sys.stderr)
            return 1
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
            )
        if args.command == "run":
            return cmd_run(runtime, args.prompt)
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
            return cmd_doctor(runtime)
        parser.error("Unsupported command")
    finally:
        runtime.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
