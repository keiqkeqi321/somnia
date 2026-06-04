from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.mcp.client import MCPClient


class MCPClientTests(unittest.TestCase):
    def test_stdio_client_uses_official_sdk_round_trip(self) -> None:
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
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "sdk-jsonl-server", "version": "0.1.0"},
                    }
                elif method == "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo input",
                                "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
                            }
                        ]
                    }
                elif method == "tools/call":
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(request.get("params", {}).get("arguments", {}), sort_keys=True),
                            }
                        ]
                    }
                else:
                    result = {}
                print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)
            """
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmpdir:
            server_script = Path(tmpdir) / "sdk_jsonl_server.py"
            server_script.write_text(server_source, encoding="utf-8")
            settings = SimpleNamespace(
                name="sdk-jsonl",
                transport="stdio",
                url=None,
                command=sys.executable,
                args=[str(server_script)],
                cwd=Path(tmpdir),
                env={},
                http_headers={},
                timeout_seconds=5,
                startup_timeout_seconds=5,
                protocol_version="2025-11-25",
            )
            client = MCPClient(settings)
            try:
                tools = client.list_tools()
                self.assertEqual(tools[0]["name"], "echo")

                result = client.call_tool("echo", {"message": "hello", "value": 7})
                text = result["content"][0]["text"]
                self.assertIn('"message": "hello"', text)
                self.assertIn('"value": 7', text)
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
