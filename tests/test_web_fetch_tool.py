from __future__ import annotations

import unittest
from unittest.mock import patch

from open_somnia.runtime.execution_mode import tool_block_message
from open_somnia.tools.registry import ToolRegistry
from open_somnia.tools.web_fetch import WebFetchError, _resolve_public_ip, register_web_fetch_tool, web_fetch


class WebFetchToolTests(unittest.TestCase):
    def test_web_fetch_converts_html_to_readable_text(self) -> None:
        body = b"""
        <!doctype html>
        <html>
          <head><style>.x{color:red}</style><script>alert(1)</script></head>
          <body><h1>Title</h1><p>Hello <b>world</b>.</p></body>
        </html>
        """
        with patch(
            "open_somnia.tools.web_fetch._fetch_following_redirects",
            return_value=("https://example.test/page", 200, "OK", "text/html; charset=utf-8", body),
        ):
            result = web_fetch(None, {"url": "https://example.test/page"})

        self.assertIn("status 200 OK · text/html; charset=utf-8", result)
        self.assertIn("Title", result)
        self.assertIn("Hello world.", result)
        self.assertNotIn("alert(1)", result)
        self.assertNotIn("color:red", result)

    def test_web_fetch_returns_plain_text_verbatim(self) -> None:
        with patch(
            "open_somnia.tools.web_fetch._fetch_following_redirects",
            return_value=("https://example.test/readme.md", 200, "OK", "text/markdown", b"# Heading\n\nBody"),
        ):
            result = web_fetch(None, {"url": "https://example.test/readme.md"})

        self.assertTrue(result.endswith("# Heading\n\nBody"))

    def test_web_fetch_blocks_local_and_cgnat_ip_literals(self) -> None:
        with self.assertRaises(WebFetchError):
            _resolve_public_ip("127.0.0.1", 80)
        with self.assertRaises(WebFetchError):
            _resolve_public_ip("100.100.100.200", 80)

    def test_web_fetch_registers_as_read_only_tool(self) -> None:
        registry = ToolRegistry()
        register_web_fetch_tool(registry)

        schemas = registry.schemas()
        self.assertEqual(schemas[0]["name"], "web_fetch")
        self.assertIsNone(tool_block_message("shortcuts", "web_fetch"))


if __name__ == "__main__":
    unittest.main()
