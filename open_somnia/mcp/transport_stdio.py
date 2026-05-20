from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import Any


class StdioTransport:
    def __init__(
        self,
        command: str,
        args: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 30,
        startup_timeout_seconds: int | None = None,
        framing: str = "jsonl",
    ):
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds if startup_timeout_seconds is not None else timeout_seconds
        normalized_framing = framing.strip().lower().replace("-", "_")
        if normalized_framing not in {"jsonl", "content_length"}:
            raise ValueError("stdio framing must be 'jsonl' or 'content_length'")
        self.framing = normalized_framing
        self.process: subprocess.Popen[bytes] | None = None
        self._responses: dict[str, Queue] = {}
        self._lock = threading.Lock()
        self.stderr_lines: list[str] = []

    def start(self) -> None:
        if self.process is not None:
            return
        process_env: dict[str, str] | None = None
        if self.env is not None:
            process_env = dict(os.environ)
            process_env.update(self.env)
        self.process = subprocess.Popen(
            [self.command, *self.args],
            cwd=self.cwd,
            env=process_env,
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
            line = stream.readline()
            if not line:
                return
            if line in {b"\r\n", b"\n"}:
                continue
            stripped = line.strip()
            if stripped.startswith(b"{"):
                self._dispatch_message(json.loads(stripped.decode("utf-8")))
                continue
            headers: dict[str, str] = {}
            while True:
                if not line:
                    return
                if line in {b"\r\n", b"\n"}:
                    break
                key, _, value = line.decode("utf-8", errors="replace").partition(":")
                headers[key.strip().lower()] = value.strip()
                line = stream.readline()
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                continue
            payload = stream.read(length)
            if not payload:
                return
            message = json.loads(payload.decode("utf-8"))
            self._dispatch_message(message)

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if msg_id is None:
            return
        queue = self._responses.get(str(msg_id))
        if queue is not None:
            queue.put(message)

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Transport not started")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with self._lock:
            if self.framing == "content_length":
                header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
                self.process.stdin.write(header)
                self.process.stdin.write(body)
            else:
                self.process.stdin.write(body)
                self.process.stdin.write(b"\n")
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
            timeout = self.startup_timeout_seconds if startup else self.timeout_seconds
            response = response_queue.get(timeout=timeout)
        except Empty as exc:
            process = self.process
            return_code = process.poll() if process is not None else None
            stderr_tail = "\n".join(self.stderr_lines[-8:]).strip()
            timeout = self.startup_timeout_seconds if startup else self.timeout_seconds
            details: list[str] = [f"MCP stdio request '{method}' timed out after {timeout}s."]
            if return_code is not None:
                details.append(f"Process exited with code {return_code}.")
            if stderr_tail:
                details.append(f"stderr:\n{stderr_tail}")
            raise RuntimeError(" ".join(details)) from exc
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
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                if stream is None:
                    continue
                try:
                    stream.close()
                except Exception:
                    pass
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except Exception:
                    pass
            wait = getattr(process, "wait", None)
            if callable(wait):
                try:
                    wait(timeout=1)
                except Exception:
                    kill = getattr(process, "kill", None)
                    if callable(kill):
                        try:
                            kill()
                        except Exception:
                            pass
