from __future__ import annotations

import contextlib
import shutil
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from open_somnia.config.backup import last_good_path
from open_somnia.config.settings import (
    APP_DIR_GITIGNORE,
    ConfigParseError,
    NoConfiguredProvidersError,
    NoUsableProvidersError,
    ensure_app_dir_ignored,
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

    def test_load_settings_normalizes_provider_model_ids_to_lowercase(self) -> None:
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
                models = ["MiMo-V2.5-Pro", "MIMO-V2.5-PRO"]
                default_model = "MiMo-V2.5-Pro"
                api_key = "openrouter-test-key"

                [model_traits."MIMO-V2.5-PRO"]
                context_window_tokens = 262144
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root, provider_override="openrouter", model_override="MIMO-V2.5-PRO")

        self.assertEqual(settings.provider.model, "mimo-v2.5-pro")
        self.assertEqual(settings.provider.context_window_tokens, 262144)
        self.assertEqual(settings.provider_profiles["openrouter"].models, ["mimo-v2.5-pro"])
        self.assertEqual(settings.provider_profiles["openrouter"].default_model, "mimo-v2.5-pro")

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
            self.assertEqual(settings.runtime.exploration_soft_limit, 10)
            self.assertEqual(settings.runtime.exploration_hard_streak_limit, 14)
            self.assertEqual(settings.runtime.exploration_hard_total_limit, 0)
            self.assertEqual((root / ".open_somnia" / ".gitignore").read_text(encoding="utf-8"), APP_DIR_GITIGNORE)

    def test_load_settings_saves_last_good_config_snapshot(self) -> None:
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
            workspace_config = root / ".open_somnia" / "open_somnia.toml"

            with self._patched_home(home):
                load_settings(root)

            self.assertEqual(last_good_path(workspace_config).read_text(encoding="utf-8"), workspace_config.read_text(encoding="utf-8"))

    def test_load_settings_recovers_invalid_workspace_toml_from_last_good(self) -> None:
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
            workspace_config = root / ".open_somnia" / "open_somnia.toml"

            with self._patched_home(home):
                load_settings(root)
                workspace_config.write_text("[providers\ninvalid = true\n", encoding="utf-8")
                settings = load_settings(root)

            self.assertEqual(settings.provider.name, "anthropic")
            self.assertIn("[providers.anthropic]", workspace_config.read_text(encoding="utf-8"))
            broken_backups = list((workspace_config.parent / "config_backups").glob("open_somnia.toml.*.broken"))
            self.assertTrue(broken_backups)
            self.assertIn("last known good", getattr(settings, "config_recovery_message", ""))

    def test_load_settings_recovers_invalid_global_toml_from_last_good(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_global_config(
                home,
                """
                [providers]
                default = "openai"

                [providers.openai]
                provider_type = "openai"
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "sk-test"
                """,
            )
            global_config = home / ".open_somnia" / "open_somnia.toml"

            with self._patched_home(home):
                load_settings(root)
                global_config.write_text("[providers\ninvalid = true\n", encoding="utf-8")
                settings = load_settings(root)

            self.assertEqual(settings.provider.name, "openai")
            self.assertIn("[providers.openai]", global_config.read_text(encoding="utf-8"))
            broken_backups = list((global_config.parent / "config_backups").glob("open_somnia.toml.*.broken"))
            self.assertTrue(broken_backups)

    def test_load_settings_without_recovery_raises_config_parse_error(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(root, "[providers")

            with self._patched_home(home):
                with self.assertRaises(ConfigParseError):
                    load_settings(root, allow_config_recovery=False)

    def test_persist_provider_selection_creates_timestamp_backup_before_write(self) -> None:
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
            workspace_config = root / ".open_somnia" / "open_somnia.toml"

            with self._patched_home(home):
                settings = load_settings(root)
                persist_provider_selection(settings, "anthropic", "claude-sonnet-4-5")

            backups = list((workspace_config.parent / "config_backups").glob("open_somnia.toml.*.bak"))
            self.assertTrue(backups)
            self.assertIn('default_model = "glm-5"', backups[0].read_text(encoding="utf-8"))
            self.assertIn('default_model = "claude-sonnet-4-5"', workspace_config.read_text(encoding="utf-8"))

    def test_ensure_app_dir_ignored_preserves_existing_gitignore(self) -> None:
        with self._tempdir() as tmpdir:
            app_dir = Path(tmpdir) / ".open_somnia"
            app_dir.mkdir(parents=True)
            gitignore = app_dir / ".gitignore"
            gitignore.write_text("custom\n", encoding="utf-8")

            ensure_app_dir_ignored(app_dir)

            self.assertEqual(gitignore.read_text(encoding="utf-8"), "custom\n")

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

        self.assertIsNone(settings.provider.reasoning_level)
        self.assertTrue(settings.provider.supports_reasoning)
        self.assertTrue(settings.provider.supports_adaptive_reasoning)
        self.assertIsNone(settings.provider_profiles["anthropic"].reasoning_level)
        self.assertTrue(settings.provider_profiles["anthropic"].model_traits["claude-sonnet-4-6"].supports_reasoning)
        self.assertTrue(
            settings.provider_profiles["anthropic"].model_traits["claude-sonnet-4-6"].supports_adaptive_reasoning
        )

    def test_load_settings_reads_openai_prompt_cache_controls(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openai"

                [providers.openai]
                provider_type = "openai"
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "openai-test-key"
                base_url = "https://api.openai.com/v1"
                prompt_cache_key = "somnia-main"
                prompt_cache_retention = "24h"
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        self.assertEqual(settings.provider.prompt_cache_key, "somnia-main")
        self.assertEqual(settings.provider.prompt_cache_retention, "24h")
        self.assertEqual(settings.provider_profiles["openai"].prompt_cache_key, "somnia-main")

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

    def test_provider_model_traits_merge_with_global_model_traits_by_field(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_global_config(
                home,
                """
                [providers]
                default = "deepseek-openai"

                [providers.deepseek-openai]
                provider_type = "openai"
                models = ["deepseek-v4-pro"]
                default_model = "deepseek-v4-pro"
                api_key = "deepseek-test-key"

                [model_traits."deepseek-v4-pro"]
                cwt = 400000
                """,
            )
            self._write_workspace_config(
                root,
                """
                [model_traits.deepseek-openai."deepseek-v4-pro"]
                reasoning_level = "deep"
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        traits = settings.provider_profiles["deepseek-openai"].model_traits["deepseek-v4-pro"]
        self.assertEqual(settings.provider.context_window_tokens, 400000)
        self.assertEqual(settings.provider.reasoning_level, "deep")
        self.assertEqual(traits.context_window_tokens, 400000)
        self.assertEqual(traits.reasoning_level, "deep")

    def test_load_settings_reads_provider_model_max_tokens(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openai"

                [providers.openai]
                provider_type = "openai"
                models = ["gpt-large", "gpt-small"]
                default_model = "gpt-large"
                api_key = "openai-test-key"
                max_tokens = 8000

                [model_traits.openai."gpt-small"]
                max_tokens = 2048
                """,
            )

            with self._patched_home(home):
                large_settings = load_settings(root, provider_override="openai", model_override="gpt-large")
                small_settings = load_settings(root, provider_override="openai", model_override="gpt-small")

        self.assertEqual(large_settings.provider.max_tokens, 8000)
        self.assertEqual(small_settings.provider.max_tokens, 2048)
        self.assertEqual(small_settings.provider_profiles["openai"].model_traits["gpt-small"].max_tokens, 2048)

    def test_load_settings_reads_provider_model_reasoning_level(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "anthropic"

                [providers.anthropic]
                models = ["claude-sonnet-4-6", "claude-haiku-4-5"]
                default_model = "claude-sonnet-4-6"
                api_key = "anthropic-test-key"
                reasoning_level = "medium"

                [model_traits.anthropic."claude-haiku-4-5"]
                reasoning_level = "low"
                """,
            )

            with self._patched_home(home):
                sonnet_settings = load_settings(root, provider_override="anthropic", model_override="claude-sonnet-4-6")
                haiku_settings = load_settings(root, provider_override="anthropic", model_override="claude-haiku-4-5")

        self.assertIsNone(sonnet_settings.provider.reasoning_level)
        self.assertEqual(haiku_settings.provider.reasoning_level, "low")
        self.assertEqual(
            haiku_settings.provider_profiles["anthropic"].model_traits["claude-haiku-4-5"].reasoning_level,
            "low",
        )

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

    def test_load_settings_can_allow_missing_provider_for_desktop_bootstrap(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            with self._patched_home(home):
                settings = load_settings(root, allow_missing_provider=True)

        self.assertEqual(settings.provider.name, "unconfigured")
        self.assertEqual(settings.provider.provider_type, "openai")
        self.assertEqual(settings.provider.model, "")
        self.assertEqual(settings.provider.api_key, "")
        self.assertEqual(settings.provider_profiles, {})

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

    def test_load_settings_allow_missing_provider_keeps_profiles_without_api_keys(self) -> None:
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
                models = ["glm-4.7"]
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root, allow_missing_provider=True)

            self.assertEqual(settings.provider.name, "glm-me")
            self.assertEqual(settings.provider.model, "glm-4.7")
            self.assertIn("glm-me", settings.provider_profiles)
            written = workspace_config.read_text(encoding="utf-8")
            self.assertIn("[providers]", written)
            self.assertIn("[providers.glm-me]", written)

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
                exploration_soft_limit = 8
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
                exploration_hard_streak_limit = 12
                exploration_hard_total_limit = 30
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
        self.assertEqual(settings.runtime.exploration_soft_limit, 8)
        self.assertEqual(settings.runtime.exploration_hard_streak_limit, 12)
        self.assertEqual(settings.runtime.exploration_hard_total_limit, 30)
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

    def test_load_settings_preserves_sse_mcp_url(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            self._write_workspace_config(
                root,
                """
                [providers]
                default = "openai"

                [providers.openai]
                models = ["gpt-4.1"]
                default_model = "gpt-4.1"
                api_key = "workspace-key"

                [mcp_servers.playwright]
                transport = "sse"
                url = "http://localhost:8931/sse"
                enabled = true
                """,
            )

            with self._patched_home(home):
                settings = load_settings(root)

        playwright = next(server for server in settings.mcp_servers if server.name == "playwright")
        self.assertEqual(playwright.transport, "sse")
        self.assertEqual(playwright.url, "http://localhost:8931/sse")
        self.assertTrue(playwright.enabled)

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

    def test_persist_provider_reasoning_level_writes_model_traits_and_roundtrips(self) -> None:
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
                persist_provider_reasoning_level(settings, "anthropic", "claude-sonnet-4-6", "medium")
                reloaded = load_settings(root)

            rendered = config_path.read_text(encoding="utf-8")
            self.assertNotIn('reasoning_level = "high"', rendered)
            self.assertIn('[model_traits.anthropic."claude-sonnet-4-6"]', rendered)
            self.assertIn('reasoning_level = "medium"', rendered)
            self.assertEqual(reloaded.provider.reasoning_level, "medium")
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

    def test_persist_provider_profile_normalizes_model_ids_to_lowercase(self) -> None:
        with self._tempdir() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            global_config = home / ".open_somnia" / "open_somnia.toml"

            with self._patched_home(home):
                persist_provider_profile(
                    "openrouter",
                    "openai",
                    ["MiMo-V2.5-Pro", "MIMO-V2.5-PRO"],
                    api_key="sk-test",
                    base_url="https://openrouter.ai/api/v1",
                )
                settings = load_settings(root)

            rendered = global_config.read_text(encoding="utf-8")
            self.assertEqual(settings.provider.model, "mimo-v2.5-pro")
            self.assertEqual(settings.provider_profiles["openrouter"].models, ["mimo-v2.5-pro"])
            self.assertIn('models = ["mimo-v2.5-pro"]', rendered)
            self.assertIn('default_model = "mimo-v2.5-pro"', rendered)

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
