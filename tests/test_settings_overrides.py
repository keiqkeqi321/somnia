from __future__ import annotations

import contextlib
import shutil
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from open_somnia.config.settings import (
    NoConfiguredProvidersError,
    NoUsableProvidersError,
    _infer_context_window_tokens,
    load_settings,
    persist_initial_provider_setup,
    persist_provider_profile,
    persist_provider_reasoning_level,
    persist_provider_selection,
)
from open_somnia.reasoning import anthropic_reasoning_payload, openai_reasoning_payload


class SettingsOverrideTests(unittest.TestCase):
    def test_openai_reasoning_payload_defaults_to_enabled_when_support_flag_is_unset(self) -> None:
        payload = openai_reasoning_payload(
            model="custom-openai-compatible-model",
            reasoning_level="high",
            supports_reasoning=None,
        )

        self.assertEqual(payload, {"reasoning": {"effort": "high"}})

    def test_anthropic_reasoning_payload_defaults_to_enabled_when_support_flag_is_unset(self) -> None:
        payload = anthropic_reasoning_payload(
            model="custom-anthropic-compatible-model",
            reasoning_level="medium",
            max_tokens=12_000,
            supports_reasoning=None,
            supports_adaptive_reasoning=None,
        )

        self.assertEqual(
            payload,
            {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": 8_192,
                }
            },
        )

    def test_reasoning_payload_still_short_circuits_when_support_flag_is_explicitly_false(self) -> None:
        openai_payload = openai_reasoning_payload(
            model="gpt-5",
            reasoning_level="high",
            supports_reasoning=False,
        )
        anthropic_payload = anthropic_reasoning_payload(
            model="claude-sonnet-4-6",
            reasoning_level="high",
            max_tokens=12_000,
            supports_reasoning=False,
            supports_adaptive_reasoning=None,
        )

        self.assertEqual(openai_payload, {})
        self.assertEqual(anthropic_payload, {})

    def test_load_settings_reads_provider_profiles_and_default_model(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["glm-5", "claude-sonnet-4-5"]
                default_model = "glm-5"
                api_key = "anthropic-test-key"
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.name, "anthropic")
        self.assertEqual(settings.provider.model, "glm-5")
        self.assertEqual(settings.provider_profiles["anthropic"].models, ["glm-5", "claude-sonnet-4-5"])

    def test_load_settings_defaults_max_agent_rounds_to_100(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["glm-5"]
                default_model = "glm-5"
                api_key = "anthropic-test-key"
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.runtime.max_agent_rounds, 100)

    def test_load_settings_can_override_provider_and_model_from_configured_profiles(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["glm-5", "claude-sonnet-4-5"]
                default_model = "glm-5"

                [providers.openai]
                models = ["gpt-4.1", "gpt-4.1-mini"]
                default_model = "gpt-4.1"
                api_key = "sk-test"
                base_url = "https://openai.example/v1"
                organization = "org-test"
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root, provider_override="openai", model_override="gpt-4.1-mini")

        self.assertEqual(settings.provider.name, "openai")
        self.assertEqual(settings.provider.model, "gpt-4.1-mini")
        self.assertEqual(settings.provider.api_key, "sk-test")
        self.assertEqual(settings.provider.base_url, "https://openai.example/v1")
        self.assertEqual(settings.provider.organization, "org-test")
        self.assertEqual(settings.provider.provider_type, "openai")

    def test_load_settings_reads_global_model_traits(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openrouter"

                [providers.openrouter]
                provider_type = "openai"
                models = ["qwen/qwen3.6-plus-preview:free"]
                default_model = "qwen/qwen3.6-plus-preview:free"
                api_key = "openrouter-test-key"

                [model_traits."qwen/qwen3.6-plus-preview:free"]
                cwt = 262144
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.context_window_tokens, 262144)
        self.assertEqual(
            settings.provider_profiles["openrouter"].model_traits["qwen/qwen3.6-plus-preview:free"].context_window_tokens,
            262144,
        )

    def test_load_settings_reads_reasoning_level_and_reasoning_model_traits(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["claude-sonnet-4-6"]
                default_model = "claude-sonnet-4-6"
                api_key = "anthropic-test-key"
                reasoning_level = "high"

                [model_traits."claude-sonnet-4-6"]
                supports_reasoning = true
                supports_adaptive_reasoning = true
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.reasoning_level, "high")
        self.assertTrue(settings.provider.supports_reasoning)
        self.assertTrue(settings.provider.supports_adaptive_reasoning)
        self.assertEqual(settings.provider_profiles["anthropic"].reasoning_level, "high")
        self.assertTrue(settings.provider_profiles["anthropic"].model_traits["claude-sonnet-4-6"].supports_reasoning)
        self.assertTrue(
            settings.provider_profiles["anthropic"].model_traits["claude-sonnet-4-6"].supports_adaptive_reasoning
        )

    def test_load_settings_provider_model_traits_override_global_model_traits(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openrouter"

                [providers.openrouter]
                provider_type = "openai"
                models = ["glm-5"]
                default_model = "glm-5"
                api_key = "openrouter-test-key"

                [providers.glm]
                provider_type = "anthropic"
                models = ["glm-5"]
                default_model = "glm-5"
                api_key = "glm-test-key"

                [model_traits."glm-5"]
                cwt = 131072

                [model_traits.glm."glm-5"]
                cwt = 262144
                """,
            )

            with self._patched_home(home):
                openrouter_settings = load_settings(root, provider_override="openrouter", model_override="glm-5")
                glm_settings = load_settings(root, provider_override="glm", model_override="glm-5")

        self.assertEqual(openrouter_settings.provider.context_window_tokens, 131072)
        self.assertEqual(glm_settings.provider.context_window_tokens, 262144)

    def test_infer_context_window_tokens_uses_official_model_mappings(self) -> None:
        self.assertEqual(_infer_context_window_tokens("openai", "minimax/MiniMax-M2.7"), 204800)
        self.assertEqual(_infer_context_window_tokens("openai", "kimi-k2.5"), 256000)
        self.assertEqual(_infer_context_window_tokens("openai", "moonshot-v1-128k"), 128000)
        self.assertEqual(_infer_context_window_tokens("openai", "qwen/qwen-plus:free"), 1000000)
        self.assertEqual(_infer_context_window_tokens("openai", "stepfun/step-3.5-flash"), 256000)
        self.assertEqual(_infer_context_window_tokens("openai", "Doubao-1-5-lite-32k"), 32000)

    def test_infer_context_window_tokens_falls_back_to_200k_for_unmapped_models(self) -> None:
        self.assertEqual(_infer_context_window_tokens("anthropic", "glm-5.1"), 200000)
        self.assertEqual(_infer_context_window_tokens("openai", "unknown-model"), 200000)

    def test_load_settings_provider_context_window_overrides_mapping(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openrouter"

                [providers.openrouter]
                provider_type = "openai"
                models = ["qwen-plus"]
                default_model = "qwen-plus"
                api_key = "openrouter-test-key"
                context_window_tokens = 65536
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.context_window_tokens, 65536)

    def test_load_settings_allows_custom_provider_name_to_map_to_openai_adapter(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openrouter"

                [providers.openrouter]
                provider_type = "openai"
                models = ["stepfun/step-3.5-flash"]
                default_model = "stepfun/step-3.5-flash"
                api_key = "sk-test"
                base_url = "https://openrouter.ai/api/v1"
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.name, "openrouter")
        self.assertEqual(settings.provider.provider_type, "openai")
        self.assertEqual(settings.provider.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(settings.provider_profiles["openrouter"].provider_type, "openai")

    def test_load_settings_raises_when_profiles_are_not_configured(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            with self._patched_home(home):
                with self.assertRaises(NoConfiguredProvidersError):
                    load_settings(root)

    def test_load_settings_clears_stale_provider_config_when_no_api_keys_exist(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            workspace_config = root / ".open_somnia" / "open_somnia.toml"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "glm-me"

                [providers.glm-me]
                default_model = "glm-4.7"

                [runtime]
                max_agent_rounds = 80
                """,
            )

            with self._patched_home(home):
                with self.assertRaises(NoUsableProvidersError):
                    load_settings(root)

            self.assertTrue(workspace_config.exists())
            written = workspace_config.read_text(encoding="utf-8")
            self.assertNotIn("[providers]", written)
            self.assertNotIn("[providers.glm-me]", written)
            self.assertIn("[runtime]", written)

    def test_load_settings_merges_global_and_workspace_configs_with_workspace_override(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_global_config(
                home,
                """
                [agent]
                name = "GlobalAgent"

                [providers]
                default = "openai"

                [providers.openai]
                models = ["gpt-4.1", "gpt-4.1-mini"]
                default_model = "gpt-4.1"
                api_key = "global-key"

                [runtime]
                max_agent_rounds = 20
                janitor_trigger_ratio = 0.65
                teammate_poll_interval_seconds = 9
                """,
            )
            self._write_workspace_config(
                root,
                """
                [agent]
                name = "WorkspaceAgent"

                [providers.openai]
                default_model = "gpt-4.1-mini"

                [runtime]
                max_agent_rounds = 80
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.agent.name, "WorkspaceAgent")
        self.assertEqual(settings.provider.name, "openai")
        self.assertEqual(settings.provider.model, "gpt-4.1-mini")
        self.assertEqual(settings.provider.api_key, "global-key")
        self.assertEqual(settings.runtime.max_agent_rounds, 80)
        self.assertEqual(settings.runtime.janitor_trigger_ratio, 0.65)
        self.assertEqual(settings.runtime.teammate_poll_interval_seconds, 9)
        self.assertEqual(settings.provider_profiles["openai"].models, ["gpt-4.1", "gpt-4.1-mini"])

    def test_load_settings_workspace_stdio_mcp_override_ignores_stale_global_http_url(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_global_config(
                home,
                """
                [providers]
                default = "openai"

                [providers.openai]
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "global-key"

                [mcp_servers.unityMCP]
                transport = "http"
                url = "http://192.168.3.161:8081/mcp"
                enabled = false
                """,
            )
            self._write_workspace_config(
                root,
                """
                [mcp_servers.unityMCP]
                transport = "stdio"
                command = "C:/Users/user/.local/bin/uvx.exe"
                args = ["--from", "mcpforunityserver==1.0.1-9.alpha", "mcp-for-unity", "--transport", "stdio"]
                enabled = true
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        unity = next(server for server in settings.mcp_servers if server.name == "unityMCP")
        self.assertEqual(unity.transport, "stdio")
        self.assertIsNone(unity.url)
        self.assertEqual(unity.command, "C:/Users/user/.local/bin/uvx.exe")
        self.assertTrue(unity.enabled)

    def test_persist_provider_selection_updates_openagent_toml_and_roundtrips(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            config_path = root / ".open_somnia" / "open_somnia.toml"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["glm-5", "kimi-k2.5"]
                default_model = "glm-5"
                api_key = "anthropic-test-key"

                [providers.openai]
                models = ["gpt-4.1", "kimi-k2.5"]
                default_model = "gpt-4.1"
                api_key = "openai-test-key"
                """,
            )
            with self._patched_home(home):
                settings = load_settings(root)

                persist_provider_selection(settings, "openai", "kimi-k2.5")
                reloaded = load_settings(root)

                self.assertEqual(reloaded.provider.name, "openai")
                self.assertEqual(reloaded.provider.model, "kimi-k2.5")
                self.assertEqual(reloaded.provider_profiles["openai"].default_model, "kimi-k2.5")
                self.assertTrue(config_path.exists())

    def test_persist_provider_reasoning_level_auto_removes_workspace_override_and_roundtrips(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            config_path = root / ".open_somnia" / "open_somnia.toml"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["claude-sonnet-4-6"]
                default_model = "claude-sonnet-4-6"
                api_key = "anthropic-test-key"
                reasoning_level = "high"
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)
                persist_provider_reasoning_level(settings, "anthropic", "auto")
                reloaded = load_settings(root)

            rendered = config_path.read_text(encoding="utf-8")
            self.assertNotIn('reasoning_level = "high"', rendered)
            self.assertIsNone(reloaded.provider.reasoning_level)
            self.assertIsNone(reloaded.provider_profiles["anthropic"].reasoning_level)

    def test_persist_initial_provider_setup_writes_global_config_and_roundtrips(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            global_config = home / ".open_somnia" / "open_somnia.toml"
            builtin_script = home / ".open_somnia" / "Hooks" / "builtin_notify" / "notify_user.py"

            with self._patched_home(home):
                written_path = persist_initial_provider_setup(
                    "openrouter",
                    "openai",
                    ["gpt-5", "gpt-4.1-mini"],
                    api_key="sk-test",
                    base_url="https://openrouter.ai/api/v1",
                )
                settings = load_settings(root)

            self.assertEqual(written_path, global_config)
            self.assertTrue(global_config.exists())
            self.assertEqual(settings.provider.name, "openrouter")
            self.assertEqual(settings.provider.provider_type, "openai")
            self.assertEqual(settings.provider.model, "gpt-5")
            self.assertEqual(settings.provider.api_key, "sk-test")
            self.assertEqual(settings.provider.base_url, "https://openrouter.ai/api/v1")
            self.assertEqual(settings.provider_profiles["openrouter"].models, ["gpt-5", "gpt-4.1-mini"])
            rendered = global_config.read_text(encoding="utf-8")
            self.assertIn('[[hooks]]', rendered)
            self.assertIn('managed_by = "somnia_builtin_notify"', rendered)
            self.assertIn('event = "TurnFailed"', rendered)
            self.assertTrue(builtin_script.exists())

    def test_persist_provider_profile_renames_existing_default_profile(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            global_config = home / ".open_somnia" / "open_somnia.toml"
            self._write_global_config(
                home,
                """
                [providers]
                default = "openrouter"

                [providers.openrouter]
                provider_type = "openai"
                models = ["gpt-5", "gpt-4.1-mini"]
                default_model = "gpt-5"
                api_key = "sk-old"
                base_url = "https://openrouter.ai/api/v1"
                """,
            )

            with self._patched_home(home):
                persist_provider_profile(
                    "openrouter-main",
                    "openai",
                    ["gpt-4.1-mini", "gpt-5"],
                    api_key="sk-new",
                    base_url="https://openrouter.ai/api/v1",
                    previous_provider_name="openrouter",
                )
                settings = load_settings(root)

            written = global_config.read_text(encoding="utf-8")

            self.assertEqual(settings.provider.name, "openrouter-main")
            self.assertEqual(settings.provider.model, "gpt-5")
            self.assertEqual(settings.provider.api_key, "sk-new")
            self.assertIn("[providers.openrouter-main]", written)
            self.assertNotIn("[providers.openrouter]", written)

    def _write_workspace_config(self, root: Path, content: str) -> None:
        config_path = root / ".open_somnia" / "open_somnia.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")

    def _write_global_config(self, home: Path, content: str) -> None:
        config_path = home / ".open_somnia" / "open_somnia.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")

    @contextlib.contextmanager
    def _tempdir(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"settings-{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        try:
            yield str(path)
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def _patched_home(self, home: Path):
        home.mkdir(parents=True, exist_ok=True)
        return patch("open_somnia.config.settings.Path.home", return_value=home)


if __name__ == "__main__":
    unittest.main()
