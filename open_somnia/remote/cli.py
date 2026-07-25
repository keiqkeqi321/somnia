from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

import uvicorn

from open_somnia.remote.connector import LocalSidecarBridge, RemoteConnector
from open_somnia.remote.identity import DeviceIdentity, default_identity_path, pair_device
from open_somnia.remote.relay import create_relay_app
from open_somnia.remote.runtime_manager import ProjectRegistry, ProjectRuntimeManager, default_registry_path


def load_relay_secret_key(value: str | None, *, required: bool) -> bytes | None:
    """Decode the Relay signing secret without exposing its value in errors."""
    normalized = str(value or "").strip()
    if not normalized:
        if required:
            raise ValueError("SOMNIA_RELAY_SECRET_KEY must be set in production.")
        return None
    try:
        padded = normalized + ("=" * (-len(normalized) % 4))
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError):
        raise ValueError("SOMNIA_RELAY_SECRET_KEY must be URL-safe Base64.") from None
    if len(decoded) != 32:
        raise ValueError("SOMNIA_RELAY_SECRET_KEY must decode to 32 bytes.")
    return decoded


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
    production = os.environ.get("SOMNIA_ENV", "").strip().lower() in {"prod", "production"}
    try:
        secret_key = load_relay_secret_key(
            os.environ.get("SOMNIA_RELAY_SECRET_KEY"),
            required=production,
        )
    except ValueError as exc:
        parser.error(str(exc))
    secure_cookies = args.secure_cookies or args.host not in {"127.0.0.1", "localhost", "::1"}
    uvicorn.run(
        create_relay_app(
            secure_cookies=secure_cookies,
            database_url=args.database_url,
            allowed_origins=args.web_origins,
            secret_key=secret_key,
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

    setup_parser = subparsers.add_parser(
        "setup",
        help="Pair this computer and optionally register a local Project in one guided step.",
    )
    setup_parser.add_argument("--relay", required=True, help="Relay HTTP(S) origin.")
    setup_parser.add_argument("--code", required=True)
    setup_parser.add_argument("--identity", type=Path, default=default_identity_path())
    setup_parser.add_argument("--project", help="Stable local Project identifier to register after pairing.")
    setup_parser.add_argument("--path", type=Path, help="Existing local Project folder.")
    setup_parser.add_argument("--name", help="Local Project display name.")
    setup_parser.add_argument("--registry", type=Path, default=default_registry_path())

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

    doctor_parser = subparsers.add_parser("doctor", help="Check local Connector readiness without exposing payloads.")
    doctor_parser.add_argument("--identity", type=Path, default=default_identity_path())
    doctor_parser.add_argument("--registry", type=Path, default=default_registry_path())
    doctor_parser.add_argument("--sidecar", help="Optional loopback Sidecar base URL to check.")
    doctor_parser.add_argument("--relay", help="Optional Relay HTTP(S) origin to check.")

    autostart_parser = subparsers.add_parser(
        "install-autostart",
        help="Generate a user-level startup entry for the paired Connector.",
    )
    autostart_parser.add_argument("--identity", type=Path, default=default_identity_path())
    autostart_parser.add_argument("--registry", type=Path, default=default_registry_path())
    autostart_parser.add_argument("--project", action="append", help="Project to expose; repeat for multiple Projects.")
    autostart_parser.add_argument("--output", type=Path, help="Override the generated startup file path.")
    args = parser.parse_args()

    if args.command == "doctor":
        return _connector_doctor(args)
    if args.command == "install-autostart":
        return _install_autostart(args)

    if args.command in {"pair", "setup"}:
        identity = DeviceIdentity.load_or_create(args.identity)
        result = pair_device(identity, relay_url=args.relay, code=args.code)
        print(f"Paired Device {result.device_name} ({result.device_id}).")
        if args.command == "setup":
            if bool(args.project) != bool(args.path):
                parser.error("setup requires --project and --path together when registering a Project.")
            if args.project and args.path:
                registration = ProjectRegistry(args.registry).register(args.project, args.path, name=args.name)
                print(f"Registered Project {registration.name} ({registration.project_id}).")
            print("Setup complete. Start the Connector with 'somnia-connector run'.")
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
    manager: ProjectRuntimeManager | None = None
    if args.sidecar:
        project_ids = args.project or ["default-project"]
        if len(project_ids) != 1:
            parser.error("--sidecar supports exactly one --project; omit --sidecar for managed Projects.")
        sidecar = LocalSidecarBridge(args.sidecar)
        sidecars: dict[str, LocalSidecarBridge] | None = None
        project_names = {project_ids[0]: project_ids[0]}
    else:
        registry = ProjectRegistry(args.registry)
        project_ids = args.project or [project.project_id for project in registry.list()]
        if not project_ids:
            parser.error("No registered Projects. Run 'somnia-connector register' before starting the Connector.")
        manager = ProjectRuntimeManager(registry)
        bridges = manager.bridges(project_ids)
        sidecar = bridges[project_ids[0]]
        sidecars = {project_id: bridge for project_id, bridge in bridges.items() if project_id != project_ids[0]}
        project_names = {project.project_id: project.name for project in registry.list() if project.project_id in project_ids}
    connector = RemoteConnector(
        relay_url,
        identity=identity,
        project_id=project_ids[0],
        sidecar=sidecar,
        sidecars=sidecars,
        project_names=project_names,
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


def _connector_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    identity_path = Path(args.identity).expanduser()
    try:
        identity = DeviceIdentity.load(identity_path)
        checks.append(("identity", True, f"loaded {identity_path}"))
        if identity.is_paired:
            checks.append(("pairing", True, f"Device {identity.device_id} is paired"))
        else:
            checks.append(("pairing", False, "Device is not paired; run 'somnia-connector setup'"))
    except Exception as exc:
        checks.append(("identity", False, _diagnostic_text(exc)))
        identity = None

    registry_path = Path(args.registry).expanduser()
    try:
        projects = ProjectRegistry(registry_path).list()
        if projects:
            missing = [project.project_id for project in projects if not project.path.exists()]
            checks.append(("projects", not missing, f"{len(projects)} registered Project(s)" if not missing else f"missing path for {', '.join(missing)}"))
        else:
            checks.append(("projects", False, "No registered Projects; run 'somnia-connector register'"))
    except Exception as exc:
        checks.append(("projects", False, _diagnostic_text(exc)))

    relay_url = str(args.relay or (identity.relay_url if identity is not None else "")).strip()
    if relay_url:
        try:
            _check_http_health(relay_url)
            checks.append(("relay", True, "health endpoint reachable"))
        except Exception as exc:
            checks.append(("relay", False, _diagnostic_text(exc)))
    else:
        checks.append(("relay", False, "Relay URL is unavailable; pair the Device first"))

    if args.sidecar:
        try:
            _check_http_health(str(args.sidecar))
            checks.append(("sidecar", True, "loopback health endpoint reachable"))
        except Exception as exc:
            checks.append(("sidecar", False, _diagnostic_text(exc)))

    for name, ok, detail in checks:
        print(f"[{ 'PASS' if ok else 'FAIL' }] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def _check_http_health(origin: str) -> None:
    normalized = str(origin).rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https.")
    request = urllib.request.Request(f"{normalized}/health", method="GET")
    with urllib.request.urlopen(request, timeout=3.0) as response:
        if response.status != 200:
            raise RuntimeError(f"health endpoint returned HTTP {response.status}")


def _install_autostart(args: argparse.Namespace) -> int:
    identity_path = Path(args.identity).expanduser().resolve()
    identity = DeviceIdentity.load(identity_path)
    if not identity.is_paired:
        print("Device is not paired; run 'somnia-connector setup' first.", file=sys.stderr)
        return 1
    registry_path = Path(args.registry).expanduser().resolve()
    projects = list(args.project or [])
    if not projects:
        projects = [project.project_id for project in ProjectRegistry(registry_path).list()]
    if not projects:
        print("No registered Projects; run 'somnia-connector register' first.", file=sys.stderr)
        return 1
    command = [
        sys.executable,
        "-m",
        "open_somnia.remote.cli",
        "connector",
        "run",
        "--identity",
        str(identity_path),
        "--registry",
        str(registry_path),
    ]
    for project in projects:
        command.extend(("--project", project))
    output = Path(args.output).expanduser() if args.output else _default_autostart_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        content = "@echo off\n" + subprocess_command(command, windows=True) + "\n"
    else:
        content = """[Unit]\nDescription=Somnia Connector\nAfter=network-online.target\n\n[Service]\nExecStart=%s\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n""" % subprocess_command(command, windows=False)
    output.write_text(content, encoding="utf-8")
    print(f"Autostart configuration written to {output}")
    if os.name != "nt":
        print("Enable it with: systemctl --user enable --now somnia-connector.service")
    return 0


def _default_autostart_path() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "somnia-connector.cmd"
    return Path.home() / ".config" / "systemd" / "user" / "somnia-connector.service"


def subprocess_command(command: list[str], *, windows: bool) -> str:
    if windows:
        return subprocess.list2cmdline(command)
    return " ".join(shlex.quote(item) for item in command)


def _diagnostic_text(exc: Exception) -> str:
    return str(exc).replace("\n", " ").strip() or exc.__class__.__name__


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"relay", "connector"}:
        print("Usage: python -m open_somnia.remote.cli {relay|connector} ...", file=sys.stderr)
        return 2
    command = sys.argv.pop(1)
    return relay_main() if command == "relay" else connector_main()


if __name__ == "__main__":
    raise SystemExit(main())
