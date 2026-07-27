from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from open_somnia.remote.identity import DeviceIdentity


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Remote Somnia test stack.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--project", default="default-project")
    parser.add_argument("--relay-port", type=int, default=8787)
    parser.add_argument("--sidecar-port", type=int, default=18765)
    parser.add_argument("--web-port", type=int, default=4173)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--rebind", action="store_true", help="Pair again even if a paired Device identity exists.")
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
        wait_ready("Web preview", web, f"http://127.0.0.1:{args.web_port}/?remote=1")
        wait_ready("Sidecar", sidecar, f"http://127.0.0.1:{args.sidecar_port}/health")

        web_url = f"http://127.0.0.1:{args.web_port}/?remote=1"
        relay_base_url = f"http://127.0.0.1:{args.relay_port}"
        paired_identity = load_paired_identity(args.identity, relay_base_url)
        skip_pairing = paired_identity is not None and not args.rebind
        if skip_pairing:
            print(f"\nDevice '{paired_identity.device_name}' is already paired with this relay; skipping pairing.")
            print("Run with -Rebind to pair this computer as a new device.")
        else:
            if args.rebind and paired_identity is not None:
                print("\nRebind requested: pairing this computer as a new device.")
            webbrowser.open(web_url)
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
                    relay_base_url,
                    "--code",
                    pairing_code,
                    "--identity",
                    str(args.identity),
                ],
                cwd=repo,
                check=True,
            )
        connector = start(
            "connector",
            [
                sys.executable,
                "-m",
                "open_somnia.remote.cli",
                "connector",
                "run",
                "--project",
                args.project,
                "--sidecar",
                f"http://127.0.0.1:{args.sidecar_port}",
                "--identity",
                str(args.identity),
            ],
            repo,
        )
        time.sleep(2)
        if connector.poll() is not None:
            hint = ""
            if skip_pairing:
                hint = " The relay may no longer recognize this device; re-run with -Rebind to pair again."
            raise RuntimeError(f"Connector exited during startup.{hint}")
        if skip_pairing:
            webbrowser.open(web_url)

        print("\nReady. Sign in again, select the newly paired Device, and Connect.")
        print("Keep this window open. Press Ctrl+C to stop the entire local stack.")
        while True:
            for name, process in (("Relay", relay), ("Web preview", web), ("Sidecar", sidecar), ("Connector", connector)):
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


def load_paired_identity(identity_path: Path, relay_url: str) -> DeviceIdentity | None:
    """Return the stored Device identity when it is already paired with this relay."""
    try:
        identity = DeviceIdentity.load(identity_path)
    except ValueError:
        return None
    if not identity.is_paired or identity.relay_url != relay_url:
        return None
    return identity


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
