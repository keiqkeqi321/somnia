"""Tests for the REPL /fork command handler (_handle_fork_command)."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.cli.repl import _handle_fork_command


class _FakeRunner:
    def __init__(self, session) -> None:
        self.session = session


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def fork_session(self, session_id: str, message_count: int):
        self.calls.append((session_id, message_count))
        return SimpleNamespace(id="forked-id")


def _session(messages: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(id="source-id", messages=messages)


class ReplForkCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _FakeRuntime()

    def _run(self, session, command: str) -> str:
        runner = _FakeRunner(session)
        out = io.StringIO()
        with redirect_stdout(out):
            _handle_fork_command(None, self.runtime, session, runner, command)
        self.runner = runner
        return out.getvalue()

    def test_explicit_count_forks_and_swaps_session(self) -> None:
        session = _session(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
            ]
        )
        out = self._run(session, "/fork 2")
        self.assertEqual(self.runtime.calls, [("source-id", 2)])
        self.assertEqual(self.runner.session.id, "forked-id")
        self.assertIn("forked session forked-id from source-id @ 2", out)

    def test_invalid_argument_does_not_fork(self) -> None:
        session = _session([{"role": "user", "content": "q1"}])
        out = self._run(session, "/fork nope")
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.runner.session.id, "source-id")
        self.assertIn("Usage", out)

    def test_out_of_range_argument_does_not_fork(self) -> None:
        session = _session([{"role": "user", "content": "q1"}])
        out = self._run(session, "/fork 9")
        self.assertEqual(self.runtime.calls, [])
        self.assertIn("between 1 and 1", out)

    def test_empty_session_does_not_fork(self) -> None:
        session = _session([])
        out = self._run(session, "/fork")
        self.assertEqual(self.runtime.calls, [])
        self.assertIn("No messages", out)

    def test_picker_selection_determines_fork_point(self) -> None:
        session = _session(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
            ]
        )
        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="2") as chooser:
            self._run(session, "/fork")
        self.assertEqual(self.runtime.calls, [("source-id", 2)])
        items = chooser.call_args.args[2]
        values = [value for value, _ in items]
        self.assertEqual(values, ["1", "2", "3", "cancel"])

    def test_picker_skips_tool_result_user_messages(self) -> None:
        session = _session(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": [{"type": "tool_call", "name": "bash", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
                {"role": "assistant", "content": "done"},
            ]
        )
        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="4") as chooser:
            self._run(session, "/fork")
        items = chooser.call_args.args[2]
        values = [value for value, _ in items]
        # The tool-call-only assistant message (#2, no text preview) and the
        # tool-result-only user message (#3) are both hidden from the picker.
        self.assertEqual(values, ["1", "4", "cancel"])
        self.assertEqual(self.runtime.calls, [("source-id", 4)])

    def test_picker_extends_boundary_past_tool_results(self) -> None:
        session = _session(
            [
                {"role": "user", "content": "q1"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "working"}, {"type": "tool_call", "name": "bash", "input": {}}],
                },
                {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
                {"role": "assistant", "content": "done"},
            ]
        )
        # Picking the tool-calling assistant message (#2) forks after its tool
        # result (#3) so the branch never starts with a dangling tool call.
        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="2"):
            self._run(session, "/fork")
        self.assertEqual(self.runtime.calls, [("source-id", 3)])

    def test_picker_cancel_does_not_fork(self) -> None:
        session = _session([{"role": "user", "content": "q1"}])
        with patch("open_somnia.cli.repl.choose_item_interactively", return_value="cancel"):
            out = self._run(session, "/fork")
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.runner.session.id, "source-id")
        self.assertIn("cancelled", out)


if __name__ == "__main__":
    unittest.main()
