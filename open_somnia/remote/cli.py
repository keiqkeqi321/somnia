from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

import uvicorn

from open_somnia.remote.connector import LocalSidecarBridge, RemoteConnector
from open_somnia.remote.identity import DeviceIdentity, default_identity_path, pair_device
from open_somnia.remote.relay import create_relay_app
from open_somnia.remote.runtime_manager import ProjectRegistry, ProjectRuntimeManager, default_registry_path


def relay_main() -> int:
    parser = argparse.ArgumentParser(description="Run the authenticated Somnia remote Relay.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--database-url", default=os.environ.get("SOMNIA_RELAY_DATABASE_URL", ""))
    parser.add_argument("--web-origin", action="append", dest="web_origins")
    parser.add_argument(
        "--secure-cookies",
        action="store_true",
        help="Mark browser cookies Secure when TLS terminates at a reverse proxy.",
    )
    args = parser.parse_args()
    if not os.environ.get("SOMNIA_ADMIN_PASSWORD", ""):
        parser.error("SOMNIA_ADMIN_PASSWORD must be set.")
    if not args.database_url:
        parser.error("SOMNIA_RELAY_DATABASE_URL or --database-url must be set.")
    secure_cookies = args.secure_cookies or args.host not in {"127.0.0.1", "localhost", "::1"}
    uvicorn.run(
        create_relay_app(
            secure_cookies=secure_cookies,
            database_url=args.database_url,
            allowed_origins=args.web_origins,
        ),
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


def connector_main() -> int:
    parser = argparse.ArgumentParser(description="Pair or run one authenticated Somnia Device Connector.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pair_parser = subparsers.add_parser("pair", help="Pair this computer with an administrator-issued code.")
    pair_parser.add_argument("--relay", required=True, help="Relay HTTP(S) origin.")
    pair_parser.add_argument("--code", required=True)
    pair_parser.add_argument("--identity", type=Path, default=default_identity_path())

    register_parser = subparsers.add_parser("register", help="Register a local Project for Connector-managed Runtime ownership.")
    register_parser.add_argument("--project", required=True, help="Stable local Project identifier.")
    register_parser.add_argument("--path", type=Path, required=True, help="Existing local Project folder.")
    register_parser.add_argument("--name", help="Local display name; defaults to the folder name.")
    register_parser.add_argument("--registry", type=Path, default=default_registry_path())

    list_parser = subparsers.add_parser("list-projects", help="List locally registered Projects.")
    list_parser.add_argument("--registry", type=Path, default=default_registry_path())

    unregister_parser = subparsers.add_parser("unregister", help="Remove a local Project registration.")
    unregister_parser.add_argument("--project", required=True)
    unregister_parser.add_argument("--registry", type=Path, default=default_registry_path())

    run_parser = subparsers.add_parser("run", help="Connect the paired Device to its Relay.")
    run_parser.add_argument("--relay", help="Relay WebSocket origin; defaults to the paired Relay.")
    run_parser.add_argument("--project", action="append", help="Registered Project to expose; repeat to expose multiple Projects.")
    run_parser.add_argument("--sidecar", help="Use an already-running loopback Sidecar (legacy direct workflow).")
    run_parser.add_argument("--registry", type=Path, default=default_registry_path())
    run_parser.add_argument("--identity", type=Path, default=default_identity_path())
    args = parser.parse_args()

    if args.command == "pair":
        identity = DeviceIdentity.load_or_create(args.identity)
        result = pair_device(identity, relay_url=args.relay, code=args.code)
        print(f"Paired Device {result.device_name} ({result.device_id}).")
        return 0

    if args.command == "register":
        registration = ProjectRegistry(args.registry).register(args.project, args.path, name=args.name)
        print(f"Registered Project {registration.name} ({registration.project_id}).")
        return 0

    if args.command == "list-projects":
        for registration in ProjectRegistry(args.registry).list():
            print(f"{registration.project_id}\t{registration.name}")
        return 0

    if args.command == "unregister":
        if not ProjectRegistry(args.registry).unregister(args.project):
            parser.error(f"Project is not registered: {args.project}")
        print(f"Unregistered Project {args.project}.")
        return 0

    identity = DeviceIdentity.load(args.identity)
    relay_url = str(args.relay or _websocket_origin(identity.relay_url))
    project_ids = args.project or ["default-project"]
    manager: ProjectRuntimeManager | None = None
    if args.sidecar:
        if len(project_ids) != 1:
            parser.error("--sidecar supports exactly one --project; omit --sidecar for managed Projects.")
        sidecar = LocalSidecarBridge(args.sidecar)
        sidecars: dict[str, LocalSidecarBridge] | None = None
    else:
        manager = ProjectRuntimeManager(ProjectRegistry(args.registry))
        bridges = manager.bridges(project_ids)
        sidecar = bridges[project_ids[0]]
        sidecars = {project_id: bridge for project_id, bridge in bridges.items() if project_id != project_ids[0]}
    connector = RemoteConnector(
        relay_url,
        identity=identity,
        project_id=project_ids[0],
        sidecar=sidecar,
        sidecars=sidecars,
    )
    try:
        connector.run()
    except KeyboardInterrupt:
        return 130
    finally:
        if manager is not None:
            manager.stop_all()
    return 0


def _websocket_origin(http_origin: str) -> str:
    parsed = urlparse(str(http_origin))
    if parsed.scheme == "http":
        return parsed._replace(scheme="ws").geturl()
    if parsed.scheme == "https":
        return parsed._replace(scheme="wss").geturl()
    raise ValueError("Paired Relay URL must use http or https.")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"relay", "connector"}:
        print("Usage: python -m open_somnia.remote.cli {relay|connector} ...", file=sys.stderr)
        return 2
    command = sys.argv.pop(1)
    return relay_main() if command == "relay" else connector_main()


if __name__ == "__main__":
    raise SystemExit(main())
