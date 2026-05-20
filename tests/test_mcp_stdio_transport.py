from __future__ import annotations

import io
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from open_somnia.mcp.transport_stdio import StdioTransport


class _FakeProcess:
    def __init__(self, *, stderr_data: bytes = b"") -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO(stderr_data)
        self._return_code = 2

    def poll(self):
        return self._return_code

    def terminate(self) -> None:
        self._return_code = 0


class MCPStdioTransportTests(unittest.TestCase):
    def test_real_minimal_stdio_server_round_trip(self) -> None:
        server_script = Path(__file__).resolve().parents[1] / "open_somnia" / "mcp" / "minimal_stdio_server.py"
        transport = StdioTransport(
            command=sys.executable,
            args=[str(server_script)],
            cwd=server_script.parents[1],
            timeout_seconds=5,
            framing="content_length",
        )
        try:
            initialize_response = transport.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "Somnia tests", "version": "0.1.0"},
                },
                startup=True,
            )
            self.assertEqual(initialize_response["result"]["serverInfo"]["name"], "somnia-minimal-stdio")

            transport.notify("notifications/initialized", {})

            tools_response = transport.request("tools/list", {})
            tool_names = {tool["name"] for tool in tools_response["result"]["tools"]}
            self.assertEqual(tool_names, {"echo", "server_info"})

            call_response = transport.request(
                "tools/call",
                {"name": "echo", "arguments": {"message": "hello", "value": 7}},
            )
            text = call_response["result"]["content"][0]["text"]
            self.assertIn('"message": "hello"', text)
            self.assertIn('"value": 7', text)
        finally:
            transport.close()

    def test_real_jsonl_stdio_server_round_trip(self) -> None:
        server_source = textwrap.dedent(
            r"""
            import json
            import sys

            for line in sys.stdin:
                request = json.loads(line)
                method = request.get("method")
                if method == "notifications/initialized":
                    continue
                if method == "initialize":
                    result = {
                        "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-11-25"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "somnia-jsonl-stdio", "version": "0.1.0"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{"name": "echo", "description": "Echo input", "inputSchema": {"type": "object"}}]}
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": json.dumps(request.get("params", {}).get("arguments", {}), sort_keys=True)}]}
                else:
                    result = {}
                print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)
            """
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmpdir:
            server_script = Path(tmpdir) / "jsonl_stdio_server.py"
            server_script.write_text(server_source, encoding="utf-8")
            transport = StdioTransport(
                command=sys.executable,
                args=[str(server_script)],
                cwd=Path(tmpdir),
                timeout_seconds=5,
            )
            try:
                initialize_response = transport.request(
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "clientInfo": {"name": "Somnia tests", "version": "0.1.0"},
                    },
                    startup=True,
                )
                self.assertEqual(initialize_response["result"]["serverInfo"]["name"], "somnia-jsonl-stdio")

                transport.notify("notifications/initialized", {})

                tools_response = transport.request("tools/list", {})
                tool_names = {tool["name"] for tool in tools_response["result"]["tools"]}
                self.assertEqual(tool_names, {"echo"})

                call_response = transport.request(
                    "tools/call",
                    {"name": "echo", "arguments": {"message": "hello", "value": 7}},
                )
                text = call_response["result"]["content"][0]["text"]
                self.assertIn('"message": "hello"', text)
                self.assertIn('"value": 7', text)
            finally:
                transport.close()

    def test_start_merges_env_with_current_process_env(self) -> None:
        fake = _FakeProcess()
        captured_kwargs = {}

        def _fake_popen(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return fake

        with patch("open_somnia.mcp.transport_stdio.subprocess.Popen", side_effect=_fake_popen):
            transport = StdioTransport(
                command="dummy",
                args=[],
                env={"SystemRoot": "C:\\WINDOWS", "UV_CACHE_DIR": "D:\\tmp\\uv-cache"},
            )
            transport.start()

        self.assertIn("env", captured_kwargs)
        process_env = captured_kwargs["env"]
        self.assertEqual(process_env["SystemRoot"], "C:\\WINDOWS")
        self.assertEqual(process_env["UV_CACHE_DIR"], "D:\\tmp\\uv-cache")
        self.assertEqual(process_env["PATH"], os.environ.get("PATH", ""))

    def test_request_timeout_includes_process_and_stderr_details(self) -> None:
        fake = _FakeProcess(stderr_data=b"uv cache permission denied\n")
        with patch("open_somnia.mcp.transport_stdio.subprocess.Popen", return_value=fake):
            transport = StdioTransport(command="dummy", args=[], timeout_seconds=0)
            transport.start()
            transport.stderr_lines.append("uv cache permission denied")

            with self.assertRaises(RuntimeError) as exc:
                transport.request("initialize", {})

        message = str(exc.exception)
        self.assertIn("timed out", message)
        self.assertIn("exited with code", message)
        self.assertIn("uv cache permission denied", message)

    def test_startup_timeout_uses_startup_timeout_seconds(self) -> None:
        fake = _FakeProcess()
        with patch("open_somnia.mcp.transport_stdio.subprocess.Popen", return_value=fake):
            transport = StdioTransport(command="dummy", args=[], timeout_seconds=99, startup_timeout_seconds=0)
            transport.start()

            with self.assertRaises(RuntimeError) as exc:
                transport.request("initialize", {}, startup=True)

        self.assertIn("timed out after 0s", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
