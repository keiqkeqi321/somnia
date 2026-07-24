from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
import httpx

from desktop.backend.server import SidecarServer
from open_somnia.remote.connector import LocalSidecarBridge, RemoteConnector
from open_somnia.remote.identity import DeviceIdentity, pair_device
from open_somnia.remote.relay import create_relay_app
from open_somnia.runtime.messages import AssistantTurn
from tests.remote_tracer_support import remote_tracer_settings, wait_until


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay-port", type=int, default=18787)
    args = parser.parse_args()
    stop = Event()

    def request_stop(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    with TemporaryDirectory() as temp_dir:
        sidecar = SidecarServer.from_settings(remote_tracer_settings(Path(temp_dir)), host="127.0.0.1", port=0)
        sidecar.runtime.complete = _streaming_complete()
        relay = uvicorn.Server(
            uvicorn.Config(
                create_relay_app(
                    administrators={"admin": "admin-password"},
                    allowed_origins=["http://127.0.0.1:4173", "http://127.0.0.1:4174"],
                ),
                host="127.0.0.1",
                port=args.relay_port,
                log_level="error",
                lifespan="off",
            )
        )
        relay_thread = Thread(target=relay.run, name="browser-test-relay", daemon=True)
        connector_errors: list[Exception] = []
        identity: DeviceIdentity | None = None

        def run_connector() -> None:
            try:
                RemoteConnector(
                    f"ws://127.0.0.1:{args.relay_port}",
                    identity=identity,
                    project_id="e2e-project",
                    sidecar=LocalSidecarBridge(sidecar.base_url),
                    project_names={"e2e-project": "Browser test project"},
                ).run(stop)
            except Exception as exc:
                if not stop.is_set():
                    connector_errors.append(exc)
                    stop.set()

        connector_thread = Thread(target=run_connector, name="browser-test-connector", daemon=True)
        try:
            sidecar.start_background()
            if not sidecar.wait_until_ready():
                raise RuntimeError("Browser fixture Sidecar did not start.")
            relay_thread.start()
            if not wait_until(lambda: relay.started):
                raise RuntimeError("Browser fixture Relay did not start.")
            relay_url = f"http://127.0.0.1:{args.relay_port}"
            with httpx.Client(base_url=relay_url) as auth_client:
                login = auth_client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "admin-password"},
                )
                if login.status_code != 200:
                    raise RuntimeError(login.text)
                code = auth_client.post("/api/pairings", json={"name": "Browser Test Device"}).json()["code"]
                identity = DeviceIdentity.load_or_create(Path(temp_dir) / "device-identity.json")
                pair_device(identity, relay_url=relay_url, code=code)
            connector_thread.start()
            time.sleep(0.2)
            if connector_errors:
                raise connector_errors[0]
            print("Somnia remote browser fixture ready.", flush=True)
            while not stop.wait(0.1):
                if connector_errors:
                    raise connector_errors[0]
        finally:
            stop.set()
            relay.should_exit = True
            sidecar.close()
            relay_thread.join(timeout=5.0)
            connector_thread.join(timeout=5.0)
    return 0


def _streaming_complete():
    final_text = """Hello remote

## Rich output

```python
print("remote")
```

```mermaid
flowchart LR
  Device --> Relay --> Web
```

![inline pixel](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=)
"""

    def complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
        del system_prompt, messages, tools, should_interrupt
        if text_callback is not None:
            text_callback("Hello ")
            time.sleep(0.75)
            text_callback(final_text.removeprefix("Hello "))
        return AssistantTurn(stop_reason="end_turn", text_blocks=[final_text])

    return complete


if __name__ == "__main__":
    raise SystemExit(main())
