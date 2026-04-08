from __future__ import annotations

import argparse

from openagent.config.settings import load_settings
from openagent.runtime.agent import OpenAgentRuntime


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
    parser = argparse.ArgumentParser(prog="openagent")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(
        args.workspace,
        provider_override=getattr(args, "provider", None),
        model_override=getattr(args, "model", None),
    )
    runtime = OpenAgentRuntime(settings)
    try:
        from openagent.cli.commands import (
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
