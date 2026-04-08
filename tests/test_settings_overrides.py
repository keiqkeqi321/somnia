from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from openagent.config.settings import load_settings, persist_provider_selection


class SettingsOverrideTests(unittest.TestCase):
    def test_load_settings_reads_provider_profiles_and_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
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
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.name, "anthropic")
        self.assertEqual(settings.provider.model, "glm-5")
        self.assertEqual(settings.provider_profiles["anthropic"].models, ["glm-5", "claude-sonnet-4-5"])

    def test_load_settings_can_override_provider_and_model_from_configured_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
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
        with tempfile.TemporaryDirectory() as tmpdir:
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

    def test_load_settings_provider_model_traits_override_global_model_traits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
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

                [providers.glm]
                provider_type = "anthropic"
                models = ["glm-5"]
                default_model = "glm-5"

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

    def test_load_settings_allows_custom_provider_name_to_map_to_openai_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
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

    def test_load_settings_falls_back_to_builtin_default_when_profiles_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.name, "anthropic")
        self.assertEqual(settings.provider.provider_type, "anthropic")
        self.assertEqual(settings.provider.model, "claude-sonnet-4-5")
        self.assertEqual(settings.provider_profiles["anthropic"].models, ["claude-sonnet-4-5"])

    def test_load_settings_merges_global_and_workspace_configs_with_workspace_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
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
        self.assertEqual(settings.runtime.teammate_poll_interval_seconds, 9)
        self.assertEqual(settings.provider_profiles["openai"].models, ["gpt-4.1", "gpt-4.1-mini"])

    def test_persist_provider_selection_updates_openagent_toml_and_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            config_path = root / ".openagent" / "openagent.toml"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["glm-5", "kimi-k2.5"]
                default_model = "glm-5"

                [providers.openai]
                models = ["gpt-4.1", "kimi-k2.5"]
                default_model = "gpt-4.1"
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

    def _write_workspace_config(self, root: Path, content: str) -> None:
        config_path = root / ".openagent" / "openagent.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")

    def _write_global_config(self, home: Path, content: str) -> None:
        config_path = home / ".openagent" / "openagent.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")

    def _patched_home(self, home: Path):
        home.mkdir(parents=True, exist_ok=True)
        return patch("openagent.config.settings.Path.home", return_value=home)


if __name__ == "__main__":
    unittest.main()
