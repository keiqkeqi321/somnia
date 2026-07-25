from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PreviewHandler(SimpleHTTPRequestHandler):
    WEB_MIME_TYPES = {
        ".css": "text/css",
        ".html": "text/html",
        ".ico": "image/x-icon",
        ".js": "text/javascript",
        ".json": "application/json",
        ".mjs": "text/javascript",
        ".wasm": "application/wasm",
    }

    def guess_type(self, path: str) -> str:
        content_type = self.WEB_MIME_TYPES.get(Path(path).suffix.lower())
        if content_type is not None:
            return content_type
        return super().guess_type(path)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        del format, args


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the built Somnia Web client on loopback.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()

    dist = Path(__file__).resolve().parents[1] / "dist"
    if not (dist / "index.html").is_file():
        parser.error("desktop/ui/dist is missing; run npm run build first.")

    handler = partial(PreviewHandler, directory=str(dist))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
