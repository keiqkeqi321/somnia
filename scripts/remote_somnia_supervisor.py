from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import json
import urllib.error
import urllib.request
import webbrowser
from http.cookiejar import CookieJar

from open_somnia.remote.identity import DeviceIdentity


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Remote Somnia test stack.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--project", default="default-project")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--managed", action="store_true", help="Run Connector-owned managed Project Runtimes.")
    parser.add_argument("--relay-port", type=int, default=8787)
    parser.add_argument("--sidecar-port", type=int, default=18765)
    parser.add_argument("--web-port", type=int, default=4173)
    parser.add_argument("--identity", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    logs = repo / ".scratch" / "remote-somnia" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[str, subprocess.Popen, object, object]] = []

    def start(name: str, command: list[str], cwd: Path) -> subprocess.Popen:
        stdout = (logs / f"{name}.out.log").open("w", encoding="utf-8")
        stderr = (logs / f"{name}.err.log").open("w", encoding="utf-8")
        process = subprocess.Popen(command, cwd=cwd, env=os.environ.copy(), stdout=stdout, stderr=stderr)
        processes.append((name, process, stdout, stderr))
        return process

    relay = start(
        "relay",
        [sys.executable, "-m", "open_somnia.remote.cli", "relay", "--host", "127.0.0.1", "--port", str(args.relay_port)],
        repo,
    )
    web = start(
        "web",
        [sys.executable, "scripts/preview_server.py", "--host", "127.0.0.1", "--port", str(args.web_port)],
        repo / "desktop" / "ui",
    )
    sidecar = None
    if not args.managed:
        sidecar = start(
            "sidecar",
            [
                sys.executable,
                "-m",
                "desktop.backend.bootstrap",
                "--workspace",
                str(args.workspace.resolve()),
                "--host",
                "127.0.0.1",
                "--port",
                str(args.sidecar_port),
                "--disable-mcp",
                "--quiet",
            ],
            repo,
        )

    try:
        wait_ready("Relay", relay, f"http://127.0.0.1:{args.relay_port}/health")
        wait_ready("Web preview", web, f"http://127.0.0.1:{args.web_port}/?remote=1&relay=http://127.0.0.1:{args.relay_port}")
        if sidecar is not None:
            wait_ready("Sidecar", sidecar, f"http://127.0.0.1:{args.sidecar_port}/health")

        # The local preview server only serves static assets; route API/WSS calls to Relay explicitly.
        web_url = f"http://127.0.0.1:{args.web_port}/?remote=1&relay=http://127.0.0.1:{args.relay_port}"
        webbrowser.open(web_url)
        relay_origin = f"http://127.0.0.1:{args.relay_port}"
        identity = _load_paired_identity(args.identity)
        if identity is not None and identity.relay_url.rstrip("/") == relay_origin and _device_is_registered(relay_origin, identity.device_id):
            print(f"\nReusing paired Device {identity.device_name} ({identity.device_id}); no new pairing code is required.")
        else:
            print("\n1. Sign in at the opened Web page.")
            print("2. Create a Device pairing code.")
            pairing_code = input("Paste the pairing code here: ").strip()
            if not pairing_code:
                raise RuntimeError("A pairing code is required.")

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "open_somnia.remote.cli",
                    "connector",
                    "pair",
                    "--relay",
                    relay_origin,
                    "--code",
                    pairing_code,
                    "--identity",
                    str(args.identity),
                ],
                cwd=repo,
                check=True,
            )
        connector_command = [
            sys.executable,
            "-m",
            "open_somnia.remote.cli",
            "connector",
            "run",
            "--identity",
            str(args.identity),
        ]
        if args.managed:
            if args.registry is None:
                raise RuntimeError("Managed Connector mode requires --registry.")
            connector_command.extend(("--registry", str(args.registry)))
        else:
            connector_command.extend(("--project", args.project, "--sidecar", f"http://127.0.0.1:{args.sidecar_port}"))
        connector = start("connector", connector_command, repo)
        time.sleep(2)
        if connector.poll() is not None:
            raise RuntimeError("Connector exited during startup.")

        print("\nReady. Keep the Web page open; it will retain your login, select the paired Device, and connect automatically when the Connector is online.")
        print("Keep this window open. Press Ctrl+C to stop the entire local stack.")
        while True:
            processes_to_check = [("Relay", relay), ("Web preview", web), ("Connector", connector)]
            if sidecar is not None:
                processes_to_check.insert(2, ("Sidecar", sidecar))
            for name, process in processes_to_check:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited unexpectedly.")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping Remote Somnia...")
        return 0
    except Exception as exc:
        print(f"\nStartup failed: {exc}", file=sys.stderr)
        print_log_tails(processes, logs)
        return 1
    finally:
        for _, process, _, _ in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for _, process, stdout, stderr in reversed(processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            stdout.close()
            stderr.close()


def wait_ready(name: str, process: subprocess.Popen, url: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited with code {process.returncode}.")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    print(f"{name} is ready.")
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"{name} did not become ready at {url}.")


def _load_paired_identity(path: Path) -> DeviceIdentity | None:
    try:
        identity = DeviceIdentity.load(path)
    except (OSError, ValueError):
        return None
    return identity if identity.is_paired else None


def _device_is_registered(relay_origin: str, device_id: str) -> bool:
    """Verify the local Relay still owns this identity before skipping pairing."""
    username = str(os.environ.get("SOMNIA_ADMIN_USERNAME", "admin")).strip()
    password = str(os.environ.get("SOMNIA_ADMIN_PASSWORD", ""))
    if not password or not device_id:
        return False
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        login = urllib.request.Request(
            f"{relay_origin.rstrip('/')}/api/auth/login",
            data=json.dumps({"username": username, "password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login, timeout=3.0):
            pass
        with opener.open(f"{relay_origin.rstrip('/')}/api/devices", timeout=3.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
            return False
        return any(isinstance(item, dict) and item.get("device_id") == device_id and not item.get("revoked_at") for item in payload["devices"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError, urllib.error.HTTPError):
        return False


def print_log_tails(processes: list[tuple[str, subprocess.Popen, object, object]], logs: Path) -> None:
    for name, _, stdout, stderr in processes:
        stdout.flush()
        stderr.flush()
        lines: list[str] = []
        for path in (logs / f"{name}.err.log", logs / f"{name}.out.log"):
            if path.exists():
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        if lines:
            print(f"\n[{name} log]", file=sys.stderr)
            print("\n".join(lines[-20:]), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
