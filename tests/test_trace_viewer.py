from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from open_somnia.cli.main import main
from open_somnia.analysis.trace_viewer import (
    build_trace_diffs,
    build_trace_viewer_report,
    load_trace_records,
    provider_payload_dir,
)


class TraceViewerTests(unittest.TestCase):
    def _write_payload(self, payload_dir: Path, name: str, payload: dict) -> None:
        payload_dir.mkdir(parents=True, exist_ok=True)
        (payload_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_trace_viewer_reports_cache_usage_and_prefix_risks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logs_dir = root / ".open_somnia" / "logs"
            payloads = provider_payload_dir(logs_dir)
            base_payload = {
                "timestamp": 1000.0,
                "session_id": "session-1",
                "actor": "lead",
                "kind": "turn",
                "provider": {"name": "openai", "type": "openai", "model": "gpt-4.1"},
                "context_usage": {"used_tokens": 1200, "max_tokens": 12000, "usage_ratio": 0.1},
                "system_prompt_sections": [
                    {"id": "core", "dynamic": False, "chars": 100, "content": "stable"},
                    {"id": "runtime", "dynamic": True, "chars": 25, "content": "dynamic"},
                ],
                "messages": [{"role": "user", "content": "hello"}],
                "message_summary": {"total": 1, "roles": {"user": 1}, "content_chars": 7},
                "tools": [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}],
                "tool_schema_summary": {"total": 1, "groups": {"filesystem": 1}},
                "payload_summary": {
                    "kind": "turn",
                    "system_prompt_chars": 125,
                    "system_prompt_section_count": 2,
                    "message_count": 1,
                    "message_content_chars": 7,
                    "tool_count": 1,
                    "max_tokens": 4096,
                    "stream": True,
                },
                "provider_request": {"body": {"model": "gpt-4.1"}},
                "provider_response": {
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 50,
                        "total_tokens": 1050,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 800,
                    },
                },
                "latency_ms": 15.5,
            }
            second_payload = dict(base_payload)
            second_payload.update(
                {
                    "timestamp": 1001.0,
                    "messages": [
                        {"role": "user", "content": "changed first message"},
                        {"role": "user", "content": "<runtime-notice>todo changed</runtime-notice>", "transient": True},
                    ],
                    "message_summary": {"total": 2, "roles": {"user": 2}, "content_chars": 64},
                    "payload_summary": {
                        **base_payload["payload_summary"],
                        "message_count": 2,
                        "message_content_chars": 64,
                    },
                    "provider_response": {
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 40,
                            "total_tokens": 1040,
                            "cache_read_input_tokens": 800,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            )
            self._write_payload(payloads, "session-1-1000-a.json", base_payload)
            self._write_payload(payloads, "session-1-1001-b.json", second_payload)

            records = load_trace_records(payloads)
            diffs = build_trace_diffs(records)
            report = build_trace_viewer_report(logs_dir)
            html = report.read_text(encoding="utf-8")

        self.assertEqual(len(records), 2)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].first_diff_message_index, 0)
        self.assertIn("message prefix changed at start", diffs[0].cache_risks)
        self.assertIn("transient/runtime notice present", diffs[0].cache_risks)
        self.assertIn("Somnia Trace Viewer", html)
        self.assertIn("session-1", html)
        self.assertIn('id="session-filter"', html)
        self.assertIn('id="metric-traces"', html)
        self.assertIn('id="metric-cache-hit"', html)
        self.assertIn("function updateMetrics()", html)
        self.assertIn('<option value="session-1">session-1</option>', html)
        self.assertIn('data-session="session-1"', html)
        self.assertIn('data-record="trace"', html)
        self.assertIn('data-input-tokens="1000"', html)
        self.assertIn('data-cache-read-tokens="800"', html)
        self.assertIn("28.6%", html)
        self.assertIn("Prompt tokens", html)
        self.assertIn("cache read 800", html)
        self.assertIn("prompt 1,800", html)
        self.assertIn("message prefix changed at start", html)
        self.assertIn("&lt;runtime-notice&gt;todo changed&lt;/runtime-notice&gt;", html)

    def test_trace_viewer_cache_hit_ratio_uses_prompt_tokens_including_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / ".open_somnia" / "logs"
            payloads = provider_payload_dir(logs_dir)
            payload = {
                "timestamp": 1000.0,
                "session_id": "session-cache",
                "kind": "turn",
                "provider": {"name": "mimo", "type": "openai", "model": "mimo-v2.5-pro"},
                "provider_response": {
                    "usage": {
                        "input_tokens": 223,
                        "output_tokens": 141,
                        "total_tokens": 364,
                        "cache_read_input_tokens": 41344,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
            self._write_payload(payloads, "session-cache-1000.json", payload)

            records = load_trace_records(payloads)
            report = build_trace_viewer_report(logs_dir)
            html = report.read_text(encoding="utf-8")

        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0].cache_hit_ratio or 0.0, 41344 / (223 + 41344))
        self.assertEqual(records[0].prompt_tokens_including_cache, 41567)
        self.assertIn("99.5%", html)
        self.assertIn("prompt 41,567", html)
        self.assertNotIn("18539.9%", html)

    def test_trace_viewer_session_filter_can_preselect_generated_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / ".open_somnia" / "logs"
            payloads = provider_payload_dir(logs_dir)
            self._write_payload(
                payloads,
                "session-a-1000.json",
                {
                    "timestamp": 1000.0,
                    "session_id": "session-a",
                    "provider_response": {"usage": {"input_tokens": 10}},
                },
            )
            self._write_payload(
                payloads,
                "session-b-1001.json",
                {
                    "timestamp": 1001.0,
                    "session_id": "session-b",
                    "provider_response": {"usage": {"input_tokens": 20}},
                },
            )

            report = build_trace_viewer_report(logs_dir, session_id="session-b")
            html = report.read_text(encoding="utf-8")

        self.assertIn('<select id="session-filter"', html)
        self.assertIn('<option value="session-b" selected>session-b</option>', html)
        self.assertIn('data-session="session-b"', html)
        self.assertNotIn('data-session="session-a"', html)

    def test_trace_viewer_handles_empty_payload_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / ".open_somnia" / "logs"
            report = build_trace_viewer_report(logs_dir)
            html = report.read_text(encoding="utf-8")

        self.assertIn("No provider payload dumps found.", html)
        self.assertIn("SOMNIA_DEBUG_PROVIDER_PAYLOADS=1", html)

    def test_cli_trace_viewer_does_not_require_configured_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            home = root / "home"
            output = root / "trace.html"
            workspace.mkdir()
            home.mkdir()

            with patch("pathlib.Path.home", return_value=home), patch("webbrowser.open") as mock_open:
                status = main(["--workspace", str(workspace), "trace-viewer", "--output", str(output)])

            html = output.read_text(encoding="utf-8")

        self.assertEqual(status, 0)
        mock_open.assert_called_once()
        self.assertIn("Somnia Trace Viewer", html)
        self.assertIn("No provider payload dumps found.", html)


if __name__ == "__main__":
    unittest.main()
