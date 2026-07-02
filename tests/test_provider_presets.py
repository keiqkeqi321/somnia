from __future__ import annotations

import unittest

from open_somnia.config.provider_presets import list_provider_presets, provider_preset_by_id, serialize_provider_preset


class ProviderPresetTests(unittest.TestCase):
    def test_provider_presets_include_requested_defaults(self) -> None:
        preset_ids = {preset.id for preset in list_provider_presets()}

        self.assertIn("deepseek", preset_ids)
        self.assertIn("glm-coding-plan", preset_ids)
        self.assertIn("bailian-token-plan", preset_ids)
        self.assertIn("mimo-token-plan", preset_ids)
        self.assertIn("kimi-coding-plan", preset_ids)
        self.assertIn("minimax-coding-plan", preset_ids)
        self.assertIn("openai", preset_ids)
        self.assertIn("anthropic", preset_ids)

    def test_provider_preset_defaults_are_valid_models(self) -> None:
        for preset in list_provider_presets():
            with self.subTest(preset=preset.id):
                self.assertIn(preset.default_model, preset.models)
                self.assertIn(preset.provider_type, {"openai", "anthropic"})
                self.assertTrue(preset.base_url)

    def test_provider_preset_serialization_matches_desktop_contract(self) -> None:
        preset = provider_preset_by_id("deepseek")
        self.assertIsNotNone(preset)

        payload = serialize_provider_preset(preset)

        self.assertEqual(payload["id"], "deepseek")
        self.assertEqual(payload["provider_type"], "openai")
        self.assertEqual(payload["base_url"], "https://api.deepseek.com")
        self.assertIsInstance(payload["models"], list)


if __name__ == "__main__":
    unittest.main()
