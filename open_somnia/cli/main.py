from __future__ import annotations

import argparse
import sys

from open_somnia.cli.prompting import (
    choose_item_interactively,
    prompt_provider_details_interactively,
)
from open_somnia.config.settings import (
    NoConfiguredProvidersError,
    global_config_path,
    load_settings,
    persist_initial_provider_setup,
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
    parser.add_argument("--workspace", default=".", help="Workspace root for the agent.")
    parser.add_argument(
        "-r",
        "-resume",
        "--resume",
        dest="resume",
        action="store_true",
        help="Open the interactive session picker and resume a saved chat.",
    )
    _add_provider_overrides(parser)
    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat", help="Start interactive chat mode.")
    chat_parser.add_argument(
        "-r",
        "-resume",
        "--resume",
        dest="resume",
        action="store_true",
        help="Open the interactive session picker and resume a saved chat.",
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
    provider_type = choose_item_interactively(
        "First Provider Setup",
        "No providers are configured yet.\nChoose the compatibility mode for your first shared profile.",
        [
            ("anthropic", "anthropic"),
            ("openai", "openai"),
        ],
    )
    if provider_type is None:
        return False

    details = prompt_provider_details_interactively(
        provider_type=provider_type,
        default_provider_name=provider_type,
        default_base_url=_default_base_url(provider_type),
    )
    if details is None:
        return False
    while True:
        provider_name = details["provider_name"].strip()
        base_url = details["base_url"].strip()
        api_key = details["api_key"].strip()
        models = _parse_model_ids(details["models"])
        if not provider_name:
            print("Provider Name is required.", file=sys.stderr)
        elif not base_url:
            print("Base URL is required.", file=sys.stderr)
        elif not api_key:
            print("API Key is required.", file=sys.stderr)
        elif not models:
            print("At least one model id is required. Use commas to separate models.", file=sys.stderr)
        else:
            break
        details = prompt_provider_details_interactively(
            provider_type=provider_type,
            default_provider_name=provider_name or provider_type,
            default_base_url=base_url or _default_base_url(provider_type),
        )
        if details is None:
            return False

    confirmation = choose_item_interactively(
        "Confirm Provider Setup",
        (
            f"Provider name: {provider_name}\n"
            f"Provider type: {provider_type}\n"
            f"Base URL: {base_url}\n"
            f"API key: {'*' * min(len(api_key), 8) if api_key else '(empty)'}\n"
            f"Models: {', '.join(models)}\n"
            f"Config file: {global_config_path()}\n"
            "Save this as the first shared provider profile?"
        ),
        [
            ("save", "Save and continue"),
            ("cancel", "Cancel"),
        ],
    )
    if confirmation != "save":
        return False

    persist_initial_provider_setup(
        provider_name,
        provider_type,
        models,
        api_key=api_key,
        base_url=base_url,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
            return cmd_chat(runtime, resume=getattr(args, "resume", False))
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
