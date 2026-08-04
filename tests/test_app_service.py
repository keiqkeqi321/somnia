from __future__ import annotations

import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

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
from open_somnia.runtime.agent import AgentLoopResult, OpenAgentRuntime
from open_somnia.runtime.compact import ContextWindowUsage
from open_somnia.runtime.events import ToolExecutionContext
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import AssistantTurn, ToolCall
from open_somnia.tools.registry import ToolDefinition


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
                state_dir=data_dir / "state",
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
            vision_provider="openai",
            vision_model="fake-model-mini",
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

    def test_context_commands_delegate_to_runtime(self) -> None:
        root = self._stable_test_dir("app-service-context-commands")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        session = service.create_session()
        calls: list[tuple[str, str]] = []

        def fake_compact(target_session):
            calls.append(("compact", target_session.id))

        def fake_janitor(target_session):
            calls.append(("janitor", target_session.id))
            return "Janitor reduced context."

        runtime.compact_session = fake_compact
        runtime.run_semantic_janitor = fake_janitor

        self.assertEqual(service.compact_session(session), "Context compacted.")
        self.assertEqual(service.run_semantic_janitor(session), "Janitor reduced context.")
        self.assertEqual(calls, [("compact", session.id), ("janitor", session.id)])

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

    def test_run_turn_emits_context_usage_updates_before_session_updated(self) -> None:
        root = self._stable_test_dir("app-service-context-usage-events")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            runtime.context_window_usage = lambda target_session: ContextWindowUsage(
                used_tokens=12_345,
                max_tokens=64_000,
                counter_name="test",
            )

            def fake_run_turn(target_session, user_input, **kwargs):
                runtime.context_window_usage(target_session)
                return AgentLoopResult("Done.", status="completed")

            runtime.run_turn = fake_run_turn

            handle = service.run_turn(session, "hello")
            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)

            events = handle.drain_events()
            event_types = [event.type for event in events]
            context_index = event_types.index("context_usage_updated")
            session_index = event_types.index("session_updated")
            self.assertLess(context_index, session_index)

            context_event = events[context_index]
            self.assertEqual(context_event.payload["context_window_usage"]["used_tokens"], 12_345)
            self.assertEqual(context_event.payload["context_window_usage"]["max_tokens"], 64_000)
            self.assertEqual(context_event.payload["context_window_usage"]["counter_name"], "test")

            session_event = events[session_index]
            self.assertEqual(session_event.payload["session"]["context_window_usage"]["used_tokens"], 12_345)
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
            event_types = [event.type for event in events]

            self.assertEqual(tool_started.payload["tool_name"], "TodoWrite")
            self.assertEqual(tool_started.payload["tool_call_id"], "call-1")
            self.assertEqual(tool_started.payload["tool_input"]["items"][0]["content"], "Build service layer")
            self.assertIn("Running", "\n".join(tool_started.payload["rendered_lines"]))
            self.assertEqual(tool_finished.payload["tool_name"], "TodoWrite")
            self.assertTrue(tool_finished.payload["log_id"])
            self.assertLess(event_types.index("tool_started"), event_types.index("tool_finished"))
            self.assertEqual(todo_updated.payload["items"][0]["status"], "in_progress")
            self.assertEqual(result.text, "Done.")
            self.assertEqual(session.todo_items[0]["content"], "Build service layer")
        finally:
            service.close()

    def test_tool_finished_event_includes_structured_content_blocks(self) -> None:
        root = self._stable_test_dir("app-service-tool-image")
        runtime = OpenAgentRuntime(self._make_settings(root))
        runtime.execution_mode = "yolo"
        runtime.registry.register(
            ToolDefinition(
                name="structured_image",
                description="Return a structured image reference.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: {
                    "status": "ok",
                    "tool_result_text": "Image ready",
                    "tool_result_content": [
                        {"type": "text", "text": "Image ready"},
                        {"type": "image_reference", "path": "qr.png", "media_type": "image/png"},
                    ],
                },
            )
        )
        service = AppService(runtime)
        try:
            session = service.create_session()
            turns = iter(
                [
                    AssistantTurn(stop_reason="tool_use", tool_calls=[ToolCall("call-1", "structured_image", {})]),
                    AssistantTurn(stop_reason="end_turn", text_blocks=["Done."]),
                ]
            )
            runtime.complete = lambda *args, **kwargs: next(turns)

            handle = service.run_turn(session, "show image")
            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)

            events = handle.drain_events()
            tool_finished = next(event for event in events if event.type == "tool_finished")
            self.assertEqual(tool_finished.payload["content_blocks"][1]["type"], "image_reference")
            self.assertEqual(tool_finished.payload["content_blocks"][1]["path"], "qr.png")
        finally:
            service.close()

    def test_worker_tool_events_are_not_emitted_to_main_turn_stream(self) -> None:
        root = self._stable_test_dir("app-service-worker-tool-events")
        runtime = OpenAgentRuntime(self._make_settings(root))
        runtime.execution_mode = "yolo"
        runtime.registry.register(
            ToolDefinition(
                name="worker_probe",
                description="Return worker output.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: "worker output",
            )
        )
        service = AppService(runtime)
        try:
            session = service.create_session()

            def fake_run_turn(session, user_input, **kwargs):
                ctx = ToolExecutionContext(
                    runtime=runtime,
                    session=session,
                    actor="Bob",
                    trace_id="bob-worker-probe",
                )
                output = runtime.registry.execute(ctx, "worker_probe", {})
                runtime.print_tool_event("Bob", "worker_probe", {}, output)
                return AgentLoopResult("Done.")

            runtime.run_turn = fake_run_turn

            handle = service.run_turn(session, "delegate worker probe")
            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)

            events = handle.drain_events()
            worker_tool_events = [
                event
                for event in events
                if event.type in {"tool_started", "tool_finished"} and event.payload.get("actor") == "Bob"
            ]
            self.assertEqual(worker_tool_events, [])

            recent_logs = runtime.tool_log_store.list_recent(limit=5)
            self.assertTrue(
                any(entry.get("actor") == "Bob" and entry.get("tool_name") == "worker_probe" for entry in recent_logs)
            )
        finally:
            service.close()

    def test_run_turn_emits_unstreamed_tool_turn_text_before_tool_events(self) -> None:
        root = self._stable_test_dir("app-service-tool-text-order")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            turns = iter(
                [
                    AssistantTurn(
                        stop_reason="tool_use",
                        text_blocks=["I need to update the todo first."],
                        tool_calls=[
                            ToolCall(
                                "call-1",
                                "TodoWrite",
                                {
                                    "items": [
                                        {
                                            "content": "Check ordering",
                                            "status": "in_progress",
                                            "activeForm": "Checking ordering",
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
            event_types = [event.type for event in events]

            self.assertLess(event_types.index("assistant_delta"), event_types.index("tool_started"))
            self.assertEqual(
                next(event for event in events if event.type == "assistant_delta").payload["delta"],
                "I need to update the todo first.",
            )
        finally:
            service.close()

    def test_run_turn_emits_subagent_activity_events(self) -> None:
        root = self._stable_test_dir("app-service-subagent-activity")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            lead_calls = 0
            subagent_calls = 0

            def fake_complete(system_prompt, messages, tools, text_callback=None, should_interrupt=None):
                nonlocal lead_calls, subagent_calls
                if system_prompt.startswith("You are an isolated subagent"):
                    subagent_calls += 1
                    if subagent_calls == 1:
                        return AssistantTurn(
                            stop_reason="tool_use",
                            text_blocks=["Scanning files."],
                            tool_calls=[ToolCall("call-2", "tree", {"path": ".", "depth": 1, "limit": 1})],
                        )
                    return AssistantTurn(stop_reason="end_turn", text_blocks=["Found the workspace root."])
                lead_calls += 1
                if lead_calls == 1:
                    return AssistantTurn(
                        stop_reason="tool_use",
                        tool_calls=[
                            ToolCall(
                                "call-1",
                                "subagent",
                                {
                                    "prompt": "Inspect the workspace",
                                    "agent_type": "Explore",
                                },
                            )
                        ],
                    )
                return AssistantTurn(stop_reason="end_turn", text_blocks=["Done."])

            runtime.complete = fake_complete

            handle = service.run_turn(session, "delegate inspection")
            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)
            events = handle.drain_events()

            subagent_events = [event for event in events if event.type == "subagent_activity"]
            self.assertTrue(any(event.payload["text"] == "Scanning files." for event in subagent_events))
            self.assertTrue(any("tree .:" in event.payload["text"] for event in subagent_events))
            self.assertTrue(any(event.payload["text"] == "Found the workspace root." for event in subagent_events))
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

    def test_run_turn_allows_two_concurrent_sessions_and_rejects_third(self) -> None:
        root = self._stable_test_dir("app-service-two-concurrent")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        release = Event()

        def fake_run_turn(runtime_self, session, user_input, **kwargs):
            release.wait(timeout=2.0)
            return AgentLoopResult(f"done {session.id}", status="completed")

        try:
            session_one = service.create_session()
            session_two = service.create_session()
            session_three = service.create_session()

            with patch.object(OpenAgentRuntime, "run_turn", fake_run_turn):
                handle_one = service.run_turn(session_one, "one")
                handle_two = service.run_turn(session_two, "two")
                self._collect_events_until(handle_one, lambda event: event.type == "turn_started")
                self._collect_events_until(handle_two, lambda event: event.type == "turn_started")

                with self.assertRaisesRegex(RuntimeError, "two turns running"):
                    service.run_turn(session_three, "three")

                release.set()
                self.assertEqual(handle_one.wait(timeout=2.0).text, f"done {session_one.id}")
                self.assertEqual(handle_two.wait(timeout=2.0).text, f"done {session_two.id}")
        finally:
            release.set()
            service.close()

    def test_run_turn_rejects_second_turn_for_same_session(self) -> None:
        root = self._stable_test_dir("app-service-same-session-concurrent")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        release = Event()

        def fake_run_turn(runtime_self, session, user_input, **kwargs):
            release.wait(timeout=2.0)
            return AgentLoopResult("done", status="completed")

        try:
            session = service.create_session()
            with patch.object(OpenAgentRuntime, "run_turn", fake_run_turn):
                handle = service.run_turn(session, "one")
                self._collect_events_until(handle, lambda event: event.type == "turn_started")
                with self.assertRaisesRegex(RuntimeError, "session already has a turn running"):
                    service.run_turn(session, "two")
                release.set()
                self.assertEqual(handle.wait(timeout=2.0).text, "done")
        finally:
            release.set()
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
            self.assertTrue(openai_models[1].is_vision)
            self.assertEqual(openai_models[1].context_window_tokens, 128_000)
        finally:
            service.close()

    def test_provider_debug_model_normalizes_model_id(self) -> None:
        root = self._stable_test_dir("app-service-provider-debug-normalizes")
        settings = self._make_settings(root)
        settings.provider_profiles["mimo"] = ProviderProfileSettings(
            name="mimo",
            provider_type="anthropic",
            models=["mimo-v2.5-pro"],
            default_model="mimo-v2.5-pro",
            api_key="fake",
            base_url="http://localhost",
        )
        runtime = OpenAgentRuntime(settings)
        service = AppService(runtime)

        class _FakeProvider:
            def complete(self, *args, **kwargs):
                return AssistantTurn(stop_reason="end_turn", text_blocks=["OK"])

        try:
            with patch.object(runtime, "_instantiate_provider", return_value=_FakeProvider()):
                result = service.debug_model_connection("mimo", "MiMo-V2.5-Pro")

            self.assertTrue(result["ok"])
            self.assertEqual(result["message"], "OK")
        finally:
            service.close()

    def test_session_provider_model_pin_is_isolated_from_workspace_default(self) -> None:
        root = self._stable_test_dir("app-service-session-model-pin")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            # Workspace default is openai / fake-model.
            self.assertEqual((runtime.settings.provider.name, runtime.settings.provider.model), ("openai", "fake-model"))

            session = service.create_session()
            self.assertIsNone(session.provider_override)
            self.assertIsNone(session.model_override)

            # Pin this session to a different model than the workspace default.
            updated = service.set_session_provider_model(session.id, "openai", "fake-model-mini")
            self.assertEqual(updated.provider_override, "openai")
            self.assertEqual(updated.model_override, "fake-model-mini")

            # The pin must not leak into the workspace-wide default.
            self.assertEqual(runtime.settings.provider.name, "openai")
            self.assertEqual(runtime.settings.provider.model, "fake-model")

            # The effective provider/model for this session reflects the pin.
            provider, model = runtime.session_effective_provider(updated)
            self.assertEqual((provider, model), ("openai", "fake-model-mini"))

            # A freshly created session still follows the workspace default.
            other = service.create_session()
            other_provider, other_model = runtime.session_effective_provider(other)
            self.assertEqual((other_provider, other_model), ("openai", "fake-model"))

            # The pin persists across a reload from disk.
            reloaded = service.load_session(session.id)
            self.assertEqual(reloaded.provider_override, "openai")
            self.assertEqual(reloaded.model_override, "fake-model-mini")

            # Clearing the pin restores default-following behavior.
            cleared = service.set_session_provider_model(session.id, None, None)
            self.assertIsNone(cleared.provider_override)
            self.assertIsNone(cleared.model_override)
            provider, model = runtime.session_effective_provider(cleared)
            self.assertEqual((provider, model), ("openai", "fake-model"))
        finally:
            service.close()

    def test_session_provider_model_pin_rejects_unknown_provider_or_model(self) -> None:
        root = self._stable_test_dir("app-service-session-model-pin-validation")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            with self.assertRaises(ValueError):
                service.set_session_provider_model(session.id, "nope", "fake-model")
            with self.assertRaises(ValueError):
                service.set_session_provider_model(session.id, "openai", "not-a-real-model")
        finally:
            service.close()

    def test_session_provider_model_pin_applies_reasoning_level(self) -> None:
        root = self._stable_test_dir("app-service-session-model-pin-reasoning")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            profile = runtime.settings.provider_profiles["openai"]
            self.assertIsNone(profile.model_traits["fake-model-mini"].reasoning_level)

            updated = service.set_session_provider_model(
                session.id,
                "openai",
                "fake-model-mini",
                reasoning_level="high",
            )
            self.assertEqual(updated.model_override, "fake-model-mini")
            # The level lands on the pinned model's traits and is persisted.
            self.assertEqual(profile.model_traits["fake-model-mini"].reasoning_level, "high")
            config_text = (root / ".open_somnia" / "open_somnia.toml").read_text(encoding="utf-8")
            self.assertIn('reasoning_level = "high"', config_text)
            # The workspace default model is untouched.
            self.assertEqual(runtime.settings.provider.model, "fake-model")
            self.assertIsNone(runtime.settings.provider.reasoning_level)

            # Clearing the level writes auto (traits None) back.
            service.set_session_provider_model(session.id, "openai", "fake-model-mini", reasoning_level="auto")
            self.assertIsNone(profile.model_traits["fake-model-mini"].reasoning_level)
        finally:
            service.close()

    def test_session_provider_model_pin_reasoning_level_validation(self) -> None:
        root = self._stable_test_dir("app-service-session-model-pin-reasoning-validation")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            profile = runtime.settings.provider_profiles["openai"]
            # A level without a pin has no model to attach to.
            with self.assertRaises(ValueError):
                service.set_session_provider_model(session.id, None, None, reasoning_level="high")
            # Unknown levels are rejected without applying the pin either.
            with self.assertRaises(ValueError):
                service.set_session_provider_model(session.id, "openai", "fake-model-mini", reasoning_level="extreme")
            self.assertIsNone(session.provider_override)
            self.assertIsNone(session.model_override)
            # Omitting the level leaves the stored traits alone.
            service.set_session_provider_model(session.id, "openai", "fake-model-mini")
            self.assertIsNone(profile.model_traits["fake-model-mini"].reasoning_level)
        finally:
            service.close()

    def test_session_provider_model_pin_reasoning_level_invalidates_cached_turn_runtime(self) -> None:
        root = self._stable_test_dir("app-service-session-pin-reasoning-cache")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)

        closed: list[str] = []

        class _StubRuntime:
            def close(self) -> None:
                closed.append("closed")

        try:
            session = service.create_session()
            cache_key = ("openai", "fake-model-mini")
            service.runtime_host._turn_runtime_cache[cache_key] = _StubRuntime()

            service.set_session_provider_model(session.id, "openai", "fake-model-mini", reasoning_level="deep")
            self.assertNotIn(cache_key, service.runtime_host._turn_runtime_cache)
            self.assertEqual(closed, ["closed"])

            # Without a reasoning level the cached runtime stays valid.
            service.runtime_host._turn_runtime_cache[cache_key] = _StubRuntime()
            service.set_session_provider_model(session.id, "openai", "fake-model-mini")
            self.assertIn(cache_key, service.runtime_host._turn_runtime_cache)
        finally:
            service.close()

    def test_set_reasoning_level_invalidates_cached_turn_runtime_for_default_pair(self) -> None:
        root = self._stable_test_dir("app-service-reasoning-cache-invalidation")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)

        class _StubRuntime:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        try:
            cache_key = ("openai", "fake-model")
            stub = _StubRuntime()
            service.runtime_host._turn_runtime_cache[cache_key] = stub

            service.set_reasoning_level("medium")

            self.assertNotIn(cache_key, service.runtime_host._turn_runtime_cache)
            self.assertTrue(stub.closed)
            self.assertEqual(runtime.settings.provider.reasoning_level, "medium")
        finally:
            service.close()

    def test_peek_turn_runtime_matches_session_pin(self) -> None:
        root = self._stable_test_dir("app-service-peek-turn-runtime")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        try:
            session = service.create_session()
            # An unpinned session peeks the primary runtime.
            self.assertIs(service.runtime_host.peek_turn_runtime(session), runtime)

            # A pinned session peeks the cached turn runtime for its pair,
            # built from the pinned model's traits (context window included).
            pinned = service.set_session_provider_model(session.id, "openai", "fake-model-mini")
            peeked = service.runtime_host.peek_turn_runtime(pinned)
            self.assertIsNot(peeked, runtime)
            self.assertEqual(peeked.settings.provider.model, "fake-model-mini")
            self.assertEqual(peeked.settings.provider.context_window_tokens, 128_000)
            # Repeated peeks share the one cached runtime turns would use.
            self.assertIs(service.runtime_host.peek_turn_runtime(pinned), peeked)
        finally:
            service.close()

    def test_run_turn_forwards_loop_injection_callbacks(self) -> None:
        root = self._stable_test_dir("app-service-loop-injection")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)
        take_calls: list[str] = []
        prepare_calls: list[str] = []

        def take_next_loop_user_message() -> str | None:
            take_calls.append("take")
            return None

        def prepare_next_loop_user_message() -> bool:
            prepare_calls.append("prepare")
            return False

        def fake_run_turn(session, user_input, **kwargs):
            self.assertEqual(user_input, "phase 2")
            kwargs["prepare_next_loop_user_message"]()
            kwargs["take_next_loop_user_message"]()
            return AgentLoopResult("Done.", status="completed")

        runtime.run_turn = fake_run_turn
        try:
            session = service.create_session()

            handle = service.run_turn(
                session,
                "phase 2",
                take_next_loop_user_message=take_next_loop_user_message,
                prepare_next_loop_user_message=prepare_next_loop_user_message,
            )
            result = handle.wait(timeout=2.0)

            self.assertIsNotNone(result)
            self.assertEqual(result.text, "Done.")
            self.assertEqual(take_calls, ["take"])
            self.assertEqual(prepare_calls, ["prepare"])
        finally:
            service.close()

    def test_service_can_queue_loop_injection_for_active_turn(self) -> None:
        root = self._stable_test_dir("app-service-active-loop-injection")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)

        def fake_run_turn(session, user_input, **kwargs):
            deadline = time.time() + 2.0
            prepared = False
            while time.time() < deadline:
                prepared = bool(kwargs["prepare_next_loop_user_message"]())
                if prepared:
                    break
                time.sleep(0.01)
            self.assertTrue(prepared)
            self.assertEqual(kwargs["take_next_loop_user_message"](), "queued follow-up")
            return AgentLoopResult("Done.", status="completed")

        runtime.run_turn = fake_run_turn
        try:
            session = service.create_session()
            handle = service.run_turn(session, "initial")
            self._collect_events_until(handle, lambda event: event.type == "turn_started")

            self.assertTrue(service.queue_loop_injection(handle.turn_id, "queued follow-up", injection_id="inject-1"))
            self.assertTrue(service.queue_loop_injection(handle.turn_id, "queued follow-up", injection_id="inject-1"))

            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)
            self.assertEqual(result.text, "Done.")
            events = handle.drain_events()
            injected_events = [event for event in events if event.type == "loop_user_message_injected"]
            self.assertEqual(len(injected_events), 1)
            self.assertEqual(injected_events[0].payload["injection_id"], "inject-1")
            self.assertEqual(injected_events[0].payload["text"], "queued follow-up")
        finally:
            service.close()

    def test_service_can_cancel_queued_loop_injection(self) -> None:
        root = self._stable_test_dir("app-service-cancel-loop-injection")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)

        def fake_run_turn(session, user_input, **kwargs):
            # Give the test thread time to queue and cancel before the boundary.
            time.sleep(0.5)
            self.assertTrue(kwargs["prepare_next_loop_user_message"]())
            self.assertEqual(kwargs["take_next_loop_user_message"](), "again")
            return AgentLoopResult("Done.", status="completed")

        runtime.run_turn = fake_run_turn
        try:
            session = service.create_session()
            handle = service.run_turn(session, "initial")
            self._collect_events_until(handle, lambda event: event.type == "turn_started")

            self.assertTrue(service.queue_loop_injection(handle.turn_id, "follow-up", injection_id="inject-1"))
            self.assertTrue(service.cancel_loop_injection(handle.turn_id, "inject-1"))
            # Cancelling an already-cancelled or unknown id reports failure.
            self.assertFalse(service.cancel_loop_injection(handle.turn_id, "inject-1"))
            # A cancelled id is free to be queued again.
            self.assertTrue(service.queue_loop_injection(handle.turn_id, "again", injection_id="inject-1"))

            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)
            self.assertEqual(result.text, "Done.")
            events = handle.drain_events()
            injected_events = [event for event in events if event.type == "loop_user_message_injected"]
            self.assertEqual(len(injected_events), 1)
            self.assertEqual(injected_events[0].payload["injection_id"], "inject-1")
            self.assertEqual(injected_events[0].payload["text"], "again")
        finally:
            service.close()

    def test_service_merges_multiple_next_loop_injections(self) -> None:
        root = self._stable_test_dir("app-service-merged-loop-injection")
        runtime = OpenAgentRuntime(self._make_settings(root))
        service = AppService(runtime)

        def fake_run_turn(session, user_input, **kwargs):
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if kwargs["prepare_next_loop_user_message"]():
                    break
                time.sleep(0.01)
            self.assertEqual(kwargs["take_next_loop_user_message"](), "first follow-up\n\nsecond follow-up")
            self.assertIsNone(kwargs["take_next_loop_user_message"]())
            return AgentLoopResult("Done.", status="completed")

        runtime.run_turn = fake_run_turn
        try:
            session = service.create_session()
            handle = service.run_turn(session, "initial")
            self._collect_events_until(handle, lambda event: event.type == "turn_started")

            self.assertTrue(service.queue_loop_injection(handle.turn_id, "first follow-up", injection_id="inject-1"))
            self.assertTrue(service.queue_loop_injection(handle.turn_id, "second follow-up", injection_id="inject-2"))

            result = handle.wait(timeout=2.0)
            self.assertIsNotNone(result)
            events = handle.drain_events()
            injected_ids = [
                event.payload["injection_id"]
                for event in events
                if event.type == "loop_user_message_injected"
            ]
            self.assertEqual(injected_ids, ["inject-1", "inject-2"])
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
