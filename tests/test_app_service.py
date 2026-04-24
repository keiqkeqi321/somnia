from __future__ import annotations

import time
import unittest
from pathlib import Path

from open_somnia.app_service import AppService
from open_somnia.config.models import (
    AgentSettings,
    AppSettings,
    ModelTraits,
    ProviderProfileSettings,
    ProviderSettings,
    RuntimeSettings,
    StorageSettings,
)
from open_somnia.runtime.agent import OpenAgentRuntime
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import AssistantTurn, ToolCall


class AppServiceTests(unittest.TestCase):
    def _stable_test_dir(self, name: str) -> Path:
        root = Path.cwd() / ".tmp-tests" / f"{name}-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _make_settings(self, root: Path) -> AppSettings:
        data_dir = root / ".open_somnia"
        transcripts_dir = data_dir / "transcripts"
        sessions_dir = data_dir / "sessions"
        tasks_dir = data_dir / "tasks"
        inbox_dir = data_dir / "inbox"
        team_dir = data_dir / "team"
        jobs_dir = data_dir / "jobs"
        requests_dir = data_dir / "requests"
        logs_dir = data_dir / "logs"
        for path in [
            data_dir,
            transcripts_dir,
            sessions_dir,
            tasks_dir,
            inbox_dir,
            team_dir,
            jobs_dir,
            requests_dir,
            logs_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)
        return AppSettings(
            workspace_root=root,
            agent=AgentSettings(name="Somnia"),
            provider=ProviderSettings(
                name="openai",
                provider_type="openai",
                model="fake-model",
                api_key="fake",
                base_url="http://localhost",
            ),
            runtime=RuntimeSettings(),
            storage=StorageSettings(
                data_dir=data_dir,
                transcripts_dir=transcripts_dir,
                sessions_dir=sessions_dir,
                tasks_dir=tasks_dir,
                inbox_dir=inbox_dir,
                team_dir=team_dir,
                jobs_dir=jobs_dir,
                requests_dir=requests_dir,
                logs_dir=logs_dir,
            ),
            provider_profiles={
                "anthropic": ProviderProfileSettings(
                    name="anthropic",
                    provider_type="anthropic",
                    models=["claude-sonnet-4-5"],
                    default_model="claude-sonnet-4-5",
                    api_key="fake",
                    base_url="http://localhost",
                ),
                "openai": ProviderProfileSettings(
                    name="openai",
                    provider_type="openai",
                    models=["fake-model", "fake-model-mini"],
                    model_traits={
                        "fake-model": ModelTraits(context_window_tokens=64_000, supports_reasoning=True),
                        "fake-model-mini": ModelTraits(context_window_tokens=128_000, supports_reasoning=False),
                    },
                    default_model="fake-model",
                    api_key="fake",
                    base_url="http://localhost",
                ),
            },
        )

    def _collect_events_until(self, handle, predicate, timeout: float = 2.0):
        deadline = time.time() + timeout
        events = []
        while time.time() < deadline:
            batch = handle.drain_events(block=True, timeout=0.05)
            if not batch:
                continue
            events.extend(batch)
            if any(predicate(event) for event in batch):
                break
        return events

    def test_run_turn_emits_stream_events_without_repl(self) -> None:
        root = self._stable_test_dir("app-service-stream")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                self.assertEqual(messages[0]["content"], "hello")
                if text_callback is not None:
                    text_callback("Hel")
                    text_callback("lo")
                return AssistantTurn(stop_reason="end_turn", text_blocks=["Hello"])

            runtime.complete = fake_complete

            handle = service.run_turn(session, "hello")
            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)

            events = handle.drain_events()
            event_types = [event.type for event in events]

            self.assertEqual(event_types[0], "turn_started")
            self.assertEqual(event_types.count("assistant_delta"), 2)
            self.assertIn("assistant_completed", event_types)
            self.assertIn("session_updated", event_types)
            self.assertEqual(result.text, "Hello")
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.messages[-1]["content"], "Hello")

            completed = next(event for event in events if event.type == "assistant_completed")
            self.assertEqual(completed.payload["text"], "Hello")
        finally:
            service.close()

    def test_run_turn_emits_tool_and_todo_events(self) -> None:
        root = self._stable_test_dir("app-service-todo")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            turns = iter(
                [
                    AssistantTurn(
                        stop_reason="tool_use",
                        tool_calls=[
                            ToolCall(
                                "call-1",
                                "TodoWrite",
                                {
                                    "items": [
                                        {
                                            "content": "Build service layer",
                                            "status": "in_progress",
                                            "activeForm": "Building service layer",
                                        }
                                    ]
                                },
                            )
                        ],
                    ),
                    AssistantTurn(stop_reason="end_turn", text_blocks=["Done."]),
                ]
            )
            runtime.complete = lambda *args, **kwargs: next(turns)

            handle = service.run_turn(session, "plan phase 1")
            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)

            events = handle.drain_events()
            tool_started = next(event for event in events if event.type == "tool_started")
            tool_finished = next(event for event in events if event.type == "tool_finished")
            todo_updated = next(event for event in events if event.type == "todo_updated")

            self.assertEqual(tool_started.payload["tool_name"], "TodoWrite")
            self.assertEqual(tool_finished.payload["tool_name"], "TodoWrite")
            self.assertTrue(tool_finished.payload["log_id"])
            self.assertEqual(todo_updated.payload["items"][0]["status"], "in_progress")
            self.assertEqual(result.text, "Done.")
            self.assertEqual(session.todo_items[0]["content"], "Build service layer")
        finally:
            service.close()

    def test_authorization_request_can_be_resolved_through_service(self) -> None:
        root = self._stable_test_dir("app-service-auth")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            turns = iter(
                [
                    AssistantTurn(
                        stop_reason="tool_use",
                        tool_calls=[
                            ToolCall(
                                "call-1",
                                "request_authorization",
                                {
                                    "tool_name": "bash",
                                    "reason": "Need to inspect git status",
                                    "argument_summary": "git status",
                                },
                            )
                        ],
                    ),
                    AssistantTurn(stop_reason="end_turn", text_blocks=["Authorized."]),
                ]
            )
            runtime.complete = lambda *args, **kwargs: next(turns)

            handle = service.run_turn(session, "inspect repo")
            events = self._collect_events_until(
                handle,
                lambda event: event.type == "authorization_requested",
            )
            request_event = next(event for event in events if event.type == "authorization_requested")

            resolved = service.resolve_authorization(
                request_event.payload["request_id"],
                scope="once",
                approved=True,
                reason="Allowed once.",
            )
            self.assertTrue(resolved)

            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)
            events.extend(handle.drain_events())

            self.assertEqual(result.text, "Authorized.")
            self.assertEqual(result.status, "completed")
            self.assertIn("tool_started", [event.type for event in events])
            self.assertIn("tool_finished", [event.type for event in events])

            tool_finished = next(
                event
                for event in events
                if event.type == "tool_finished" and event.payload["tool_name"] == "request_authorization"
            )
            self.assertIn('"scope":"once"', tool_finished.payload["output"].replace(" ", ""))
        finally:
            service.close()

    def test_interrupt_turn_emits_interrupt_events(self) -> None:
        root = self._stable_test_dir("app-service-interrupt")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                while should_interrupt is not None and not should_interrupt():
                    time.sleep(0.01)
                raise TurnInterrupted("Interrupted by user.")

            runtime.complete = fake_complete

            handle = service.run_turn(session, "long running task")
            self._collect_events_until(handle, lambda event: event.type == "turn_started")
            self.assertTrue(service.interrupt_turn(handle.turn_id))

            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)
            events = handle.drain_events()
            event_types = [event.type for event in events]

            self.assertIn("interrupt_requested", event_types)
            self.assertIn("interrupt_completed", event_types)
            self.assertTrue(result.interrupted)
            self.assertEqual(result.status, "interrupted")
        finally:
            service.close()

    def test_provider_service_lists_providers_and_models(self) -> None:
        root = self._stable_test_dir("app-service-providers")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            providers = service.list_providers()
            openai_models = service.list_models("openai")

            self.assertEqual([provider.name for provider in providers], ["anthropic", "openai"])
            self.assertEqual([model.name for model in openai_models], ["fake-model", "fake-model-mini"])
            self.assertTrue(openai_models[0].is_default)
            self.assertTrue(openai_models[0].is_active)
            self.assertEqual(openai_models[1].context_window_tokens, 128_000)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
