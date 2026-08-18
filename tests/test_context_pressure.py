"""Context-pressure early-warning messages (persisted, not transient).

Auto-compaction at 0.82 is the lossy backstop; the pressure bands below it
append a real user message once per episode so the model can finish the
stage and hand off via request_new_session before detail is lost.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.compact import (
    CONTEXT_PRESSURE_SOFT_RATIO,
    CONTEXT_PRESSURE_URGENT_RATIO,
    ContextWindowUsage,
    context_pressure_level,
)
from open_somnia.runtime.session import AgentSession


def _usage(ratio: float | None, *, counter: str = "tiktoken") -> ContextWindowUsage:
    if ratio is None:
        return ContextWindowUsage(used_tokens=12_345, max_tokens=None, counter_name=counter)
    max_tokens = 100_000
    return ContextWindowUsage(
        used_tokens=int(max_tokens * ratio),
        max_tokens=max_tokens,
        counter_name=counter,
    )


def _make_runtime(usage: ContextWindowUsage) -> tuple[OpenAgentRuntime, list[dict]]:
    runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
    runtime.settings = SimpleNamespace(
        runtime=SimpleNamespace(
            context_pressure_soft_ratio=CONTEXT_PRESSURE_SOFT_RATIO,
            context_pressure_urgent_ratio=CONTEXT_PRESSURE_URGENT_RATIO,
        )
    )
    runtime.context_window_usage = lambda session: usage  # type: ignore[method-assign]
    transcript: list[dict] = []
    runtime._append_transcript_entry = lambda session_id, entry: transcript.append(entry)  # type: ignore[method-assign]
    return runtime, transcript


def _pressure_messages(session: AgentSession) -> list[str]:
    return [
        str(message.get("content", ""))
        for message in session.messages
        if str(message.get("content", "")).startswith("<context-pressure")
    ]


class ContextPressureLevelTests(unittest.TestCase):
    def test_healthy_and_unknown_window_report_no_pressure(self) -> None:
        self.assertIsNone(context_pressure_level(_usage(0.50)))
        self.assertIsNone(context_pressure_level(_usage(None)))

    def test_bands_match_thresholds(self) -> None:
        self.assertIsNone(context_pressure_level(_usage(CONTEXT_PRESSURE_SOFT_RATIO - 0.01)))
        self.assertEqual(context_pressure_level(_usage(CONTEXT_PRESSURE_SOFT_RATIO)), "soft")
        self.assertEqual(context_pressure_level(_usage(CONTEXT_PRESSURE_URGENT_RATIO - 0.01)), "soft")
        self.assertEqual(context_pressure_level(_usage(CONTEXT_PRESSURE_URGENT_RATIO)), "urgent")

    def test_ratios_can_be_overridden(self) -> None:
        usage = _usage(0.55)
        self.assertEqual(context_pressure_level(usage, soft_ratio=0.50, urgent_ratio=0.60), "soft")
        self.assertEqual(context_pressure_level(usage, soft_ratio=0.40, urgent_ratio=0.50), "urgent")

    def test_hard_trigger_is_owned_by_compaction_not_the_warning(self) -> None:
        self.assertIsNone(context_pressure_level(_usage(0.82)))
        self.assertIsNone(context_pressure_level(_usage(0.95)))


class ContextPressureMessageTests(unittest.TestCase):
    def test_soft_crossing_appends_one_persisted_user_message(self) -> None:
        runtime, transcript = _make_runtime(_usage(0.72))
        session = AgentSession(id="session-1", messages=[{"role": "user", "content": "hi"}])

        runtime._maybe_append_context_pressure_message(session)
        runtime._maybe_append_context_pressure_message(session)

        messages = _pressure_messages(session)
        self.assertEqual(len(messages), 1)
        self.assertIn('level="soft"', messages[0])
        self.assertIn("request_new_session", messages[0])
        self.assertEqual(session.messages[-1]["role"], "user")
        self.assertEqual(len(transcript), 1)

    def test_escalation_to_urgent_appends_a_second_message(self) -> None:
        runtime, transcript = _make_runtime(_usage(0.72))
        session = AgentSession(id="session-1")

        runtime._maybe_append_context_pressure_message(session)
        runtime.context_window_usage = lambda session: _usage(0.80)  # type: ignore[method-assign]
        runtime._maybe_append_context_pressure_message(session)
        runtime._maybe_append_context_pressure_message(session)

        messages = _pressure_messages(session)
        self.assertEqual(len(messages), 2)
        self.assertIn('level="soft"', messages[0])
        self.assertIn('level="urgent"', messages[1])
        self.assertIn("imminent", messages[1])
        self.assertEqual(len(transcript), 2)

    def test_healthy_usage_resets_the_episode_so_recrossing_warns_again(self) -> None:
        runtime, _transcript = _make_runtime(_usage(0.75))
        session = AgentSession(id="session-1")

        runtime._maybe_append_context_pressure_message(session)
        runtime.context_window_usage = lambda session: _usage(0.40)  # type: ignore[method-assign]
        runtime._maybe_append_context_pressure_message(session)
        runtime.context_window_usage = lambda session: _usage(0.75)  # type: ignore[method-assign]
        runtime._maybe_append_context_pressure_message(session)

        self.assertEqual(len(_pressure_messages(session)), 2)

    def test_healthy_usage_never_appends(self) -> None:
        runtime, transcript = _make_runtime(_usage(0.30))
        session = AgentSession(id="session-1")

        runtime._maybe_append_context_pressure_message(session)

        self.assertEqual(_pressure_messages(session), [])
        self.assertEqual(transcript, [])

    def test_usage_failure_is_silent(self) -> None:
        runtime = OpenAgentRuntime.__new__(OpenAgentRuntime)
        runtime.settings = SimpleNamespace(runtime=SimpleNamespace())
        def _boom(session):
            raise RuntimeError("no provider")
        runtime.context_window_usage = _boom  # type: ignore[method-assign]
        session = AgentSession(id="session-1")

        runtime._maybe_append_context_pressure_message(session)

        self.assertEqual(session.messages, [])


if __name__ == "__main__":
    unittest.main()
