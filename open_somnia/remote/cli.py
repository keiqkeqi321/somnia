from __future__ import annotations

import argparse

import uvicorn

from open_somnia.remote.connector import LocalSidecarBridge, RemoteConnector
from open_somnia.remote.relay import create_relay_app


def relay_main() -> int:
    parser = argparse.ArgumentParser(description="Run the stateless Somnia remote Relay tracer.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    uvicorn.run(create_relay_app(), host=args.host, port=args.port, log_level="info")
    return 0


def connector_main() -> int:
    parser = argparse.ArgumentParser(description="Connect one local Somnia Project to the remote Relay tracer.")
    parser.add_argument("--relay", default="ws://127.0.0.1:8787")
    parser.add_argument("--device", default="local-device")
    parser.add_argument("--project", default="default-project")
    parser.add_argument("--sidecar", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    connector = RemoteConnector(
        args.relay,
        device_id=args.device,
        project_id=args.project,
        sidecar=LocalSidecarBridge(args.sidecar),
    )
    try:
        connector.run()
    except KeyboardInterrupt:
        return 130
    return 0
