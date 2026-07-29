from __future__ import annotations

import argparse
import json
import sys

from desktop.backend.instance_lock import SidecarInstanceLockError
from desktop.backend.server import SidecarServer
from open_somnia.config.settings import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="somnia-sidecar")
    parser.add_argument("--workspace", default=".", help="Workspace root for the sidecar runtime.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8765, help="TCP port to bind. Use 0 for an ephemeral port.")
    parser.add_argument(
        "--provider",
        default=argparse.SUPPRESS,
        help="Override the configured provider profile for this sidecar process.",
    )
    parser.add_argument(
        "--model",
        default=argparse.SUPPRESS,
        help="Override the configured model for this sidecar process.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print the startup readiness payload to stdout.",
    )
    parser.add_argument(
        "--disable-mcp",
        action="store_true",
        help="Skip MCP server initialization for a constrained local runtime.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(
        args.workspace,
        provider_override=getattr(args, "provider", None),
        model_override=getattr(args, "model", None),
        allow_missing_provider=True,
    )
    if args.disable_mcp:
        settings.mcp_servers = []

    try:
        server = SidecarServer.from_settings(settings, host=args.host, port=args.port)
    except SidecarInstanceLockError as exc:
        # A live sidecar already serves this workspace; exit quietly instead of
        # fighting over it. The launcher will adopt the existing one.
        print(f"somnia-sidecar: {exc}", file=sys.stderr)
        return 0
    try:
        if not args.quiet:
            print(json.dumps(server.ready_payload(), ensure_ascii=False))
            sys.stdout.flush()
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
