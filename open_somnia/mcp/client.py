from __future__ import annotations

import io
import threading
import tempfile
from contextlib import ExitStack
from datetime import timedelta
from typing import Any

import httpx
from anyio import fail_after
from anyio.from_thread import start_blocking_portal
from mcp import ClientSession, Implementation, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from open_somnia.config.models import MCPServerSettings


class _StderrBuffer(io.TextIOBase):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        chunk = str(text)
        if not chunk:
            return 0
        with self._lock:
            self._file.write(chunk)
            self._file.flush()
        return len(chunk)

    def tail(self, line_count: int = 8) -> str:
        with self._lock:
            self._file.flush()
            self._file.seek(0)
            joined = self._file.read()
        lines = [line.rstrip() for line in joined.splitlines() if line.strip()]
        return "\n".join(lines[-line_count:])

    def fileno(self) -> int:
        return self._file.fileno()

    def close(self) -> None:
        with self._lock:
            self._file.close()
        super().close()


class MCPClient:
    def __init__(self, settings: MCPServerSettings):
        self.settings = settings
        self.initialized = False
        self._session: ClientSession | None = None
        self._portal = None
        self._portal_manager = None
        self._exit_stack: ExitStack | None = None
        self._lock = threading.RLock()
        self._stderr_buffer = _StderrBuffer() if settings.transport == "stdio" else None

    def initialize(self) -> None:
        with self._lock:
            if self.initialized:
                return
            self._portal_manager = start_blocking_portal()
            self._portal = self._portal_manager.__enter__()
            try:
                self._initialize_session()
            except Exception as exc:
                self.close()
                raise self._wrap_error("initialize", exc) from exc
            self.initialized = True

    def _initialize_session(self) -> None:
        portal = self._portal
        if portal is None:
            raise RuntimeError("MCP portal is not initialized")
        stack = ExitStack()
        self._exit_stack = stack
        read_stream: Any
        write_stream: Any
        if self.settings.transport == "http":
            if not self.settings.url:
                raise ValueError(f"MCP server '{self.settings.name}' requires a url for http transport")
            http_client = stack.enter_context(
                portal.wrap_async_context_manager(
                    httpx.AsyncClient(
                        headers=self.settings.http_headers or None,
                        timeout=httpx.Timeout(float(max(self.settings.timeout_seconds, self.settings.startup_timeout_seconds))),
                    )
                )
            )
            read_stream, write_stream, _ = stack.enter_context(
                portal.wrap_async_context_manager(streamable_http_client(self.settings.url, http_client=http_client))
            )
        elif self.settings.transport == "sse":
            if not self.settings.url:
                raise ValueError(f"MCP server '{self.settings.name}' requires a url for sse transport")
            read_stream, write_stream = stack.enter_context(
                portal.wrap_async_context_manager(
                    sse_client(
                        self.settings.url,
                        headers=self.settings.http_headers or None,
                        timeout=float(self.settings.timeout_seconds),
                        sse_read_timeout=float(max(self.settings.timeout_seconds, self.settings.startup_timeout_seconds)),
                    )
                )
            )
        else:
            server = StdioServerParameters(
                command=self.settings.command,
                args=self.settings.args,
                cwd=self.settings.cwd,
                env=self.settings.env or None,
            )
            read_stream, write_stream = stack.enter_context(
                portal.wrap_async_context_manager(stdio_client(server, errlog=self._stderr_buffer or io.StringIO()))
            )
        self._session = stack.enter_context(
            portal.wrap_async_context_manager(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self.settings.timeout_seconds),
                    client_info=Implementation(name="Somnia", version="0.1.0"),
                )
            )
        )
        portal.call(self._async_initialize_session)

    async def _async_initialize_session(self) -> None:
        if self._session is None:
            raise RuntimeError("MCP session is not initialized")
        with fail_after(self.settings.startup_timeout_seconds):
            await self._session.initialize()

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        try:
            result = self._call_session("list_tools")
        except Exception as exc:
            raise self._wrap_error("tools/list", exc) from exc
        return [tool.model_dump(by_alias=True, mode="json", exclude_none=True) for tool in result.tools]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        try:
            result = self._call_session("call_tool", name, arguments)
        except Exception as exc:
            raise self._wrap_error("tools/call", exc) from exc
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)

    def _call_session(self, method_name: str, *args: Any) -> Any:
        session = self._session
        portal = self._portal
        if session is None or portal is None:
            raise RuntimeError("MCP session is not initialized")
        method = getattr(session, method_name)
        return portal.call(method, *args)

    def close(self) -> None:
        with self._lock:
            self.initialized = False
            exit_stack = self._exit_stack
            self._session = None
            self._exit_stack = None
            if exit_stack is not None:
                try:
                    exit_stack.close()
                except Exception:
                    pass
            self._close_portal()

    def _close_portal(self) -> None:
        portal_manager = self._portal_manager
        self._portal = None
        self._portal_manager = None
        if portal_manager is not None:
            portal_manager.__exit__(None, None, None)

    def _wrap_error(self, action: str, exc: Exception) -> RuntimeError:
        if isinstance(exc, RuntimeError):
            message = str(exc)
        elif isinstance(exc, McpError):
            error = exc.error
            message = str(error.message or action)
            if error.data not in (None, ""):
                message = f"{message} ({error.data})"
        else:
            message = str(exc) or exc.__class__.__name__
        stderr_tail = self._stderr_buffer.tail() if self._stderr_buffer is not None else ""
        if stderr_tail and stderr_tail not in message:
            message = f"{message}\nstderr:\n{stderr_tail}"
        return RuntimeError(message)
