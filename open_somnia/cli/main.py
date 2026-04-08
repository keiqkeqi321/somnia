from __future__ import annotations

import argparse
import sys

from open_somnia import __version__
from open_somnia.cli.provider_management import (
    choose_provider_target_interactively,
    collect_provider_profile_interactively,
    default_base_url,
    parse_model_ids,
)
from open_somnia.config.settings import (
    NoConfiguredProvidersError,
    NoUsableProvidersError,
    global_config_path,
    load_settings,
    persist_initial_provider_setup,
    persist_provider_profile,
)
from open_somnia.runtime.agent import OpenAgentRuntime


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
    subparsers.add_parser("providers", help="Add or edit shared provider profiles.")
    return parser


def _can_prompt_interactively() -> bool:
    stdin = getattr(sys.stdin, "isatty", None)
    stdout = getattr(sys.stdout, "isatty", None)
    return bool(callable(stdin) and stdin() and callable(stdout) and stdout())


def _parse_model_ids(raw_value: str) -> list[str]:
    return parse_model_ids(raw_value)


def _default_base_url(provider_type: str) -> str:
    return default_base_url(provider_type)


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "providers":
        return _manage_providers(args.workspace)
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
