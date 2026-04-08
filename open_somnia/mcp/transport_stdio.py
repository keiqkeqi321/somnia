from __future__ import annotations

import json
import subprocess
import threading
import uuid
from pathlib import Path
from queue import Queue
from typing import Any


class StdioTransport:
    def __init__(
        self,
        command: str,
        args: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 30,
    ):
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[bytes] | None = None
        self._responses: dict[str, Queue] = {}
        self._lock = threading.Lock()
        self.stderr_lines: list[str] = []

    def start(self) -> None:
        if self.process is not None:
            return
        self.process = subprocess.Popen(
            [self.command, *self.args],
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        for raw in iter(self.process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self.stderr_lines.append(line)

    def _read_stdout(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        stream = self.process.stdout
        while True:
            headers: dict[str, str] = {}
            while True:
                line = stream.readline()
                if not line:
                    return
                if line in {b"\r\n", b"\n"}:
                    break
                key, _, value = line.decode("utf-8", errors="replace").partition(":")
                headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if length <= 0:
                continue
            payload = stream.read(length)
            if not payload:
                return
            message = json.loads(payload.decode("utf-8"))
            msg_id = message.get("id")
            if msg_id is None:
                continue
            queue = self._responses.get(str(msg_id))
            if queue is not None:
                queue.put(message)

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Transport not started")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        with self._lock:
            self.process.stdin.write(header)
            self.process.stdin.write(body)
            self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, *, startup: bool = False) -> dict[str, Any]:
        self.start()
        msg_id = uuid.uuid4().hex[:8]
        response_queue: Queue = Queue()
        self._responses[msg_id] = response_queue
        self._write(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
        )
        try:
            response = response_queue.get(timeout=self.timeout_seconds)
        finally:
            self._responses.pop(msg_id, None)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.start()
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None:
            process.terminate()
