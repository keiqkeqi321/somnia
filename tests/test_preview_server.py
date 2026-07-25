from __future__ import annotations

import importlib.util
import io
import unittest
from pathlib import Path


PREVIEW_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "desktop" / "ui" / "scripts" / "preview_server.py"
)
SPEC = importlib.util.spec_from_file_location("somnia_preview_server", PREVIEW_SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
PREVIEW_SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREVIEW_SERVER)


class PreviewHandlerTests(unittest.TestCase):
    def test_web_assets_use_browser_compatible_mime_types(self) -> None:
        handler = PREVIEW_SERVER.PreviewHandler.__new__(PREVIEW_SERVER.PreviewHandler)

        expected_types = {
            "app.js": "text/javascript",
            "worker.mjs": "text/javascript",
            "app.css": "text/css",
            "manifest.json": "application/json",
            "module.wasm": "application/wasm",
        }
        for path, expected in expected_types.items():
            with self.subTest(path=path):
                self.assertEqual(handler.guess_type(path), expected)

    def test_responses_disable_browser_cache(self) -> None:
        handler = PREVIEW_SERVER.PreviewHandler.__new__(PREVIEW_SERVER.PreviewHandler)
        handler.wfile = io.BytesIO()
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"
        handler.requestline = "GET /assets/app.js HTTP/1.1"
        handler.send_response(200)
        handler.end_headers()

        self.assertIn(b"Cache-Control: no-store\r\n", handler.wfile.getvalue())


if __name__ == "__main__":
    unittest.main()
