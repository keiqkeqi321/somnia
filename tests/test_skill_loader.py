from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from open_somnia.skills.loader import DEFAULT_SKILL_PROMPT_DESCRIPTION_CHARS, SkillLoader


class SkillLoaderTests(unittest.TestCase):
    def test_workspace_skill_overrides_global_and_finds_case_insensitive_skill_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            global_dir = root / "global"
            workspace_dir = root / "workspace"
            (global_dir / "Unity" ).mkdir(parents=True)
            (workspace_dir / "Unity").mkdir(parents=True)
            (global_dir / "Unity" / "skill.md").write_text(
                "---\ndescription: global desc\n---\nglobal body\n",
                encoding="utf-8",
            )
            (workspace_dir / "Unity" / "SKILL.md").write_text(
                "---\ndescription: workspace desc\n---\nworkspace body\n",
                encoding="utf-8",
            )

            loader = SkillLoader([global_dir, workspace_dir])

            self.assertEqual(loader.names(), ["Unity"])
            self.assertIn("workspace body", loader.load("unity"))
            self.assertIn("workspace desc", loader.descriptions())

    def test_render_listing_includes_scope_and_usage_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skills_dir = root / ".open_somnia" / "skills"
            (skills_dir / "Review").mkdir(parents=True)
            (skills_dir / "Review" / "Skill.MD").write_text(
                "---\ndescription: review code\n---\nbody\n",
                encoding="utf-8",
            )

            loader = SkillLoader([skills_dir])
            rendered = loader.render_listing()

            self.assertIn("- Review [workspace] - review code", rendered)
            self.assertIn("use: /+Review", rendered)

    def test_for_workspace_includes_builtin_somnia_config_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = SkillLoader.for_workspace(Path(tmpdir))

            self.assertIn("somnia-config", loader.names())
            self.assertIn("TOML configuration", loader.descriptions())
            self.assertIn("Complete TOML Map", loader.load("somnia-config"))

    def test_prompt_index_shows_names_without_descriptions_after_described_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skills_dir = root / "skills"
            for name in ["Alpha", "Beta", "Gamma"]:
                skill_dir = skills_dir / name
                skill_dir.mkdir(parents=True)
                skill_dir.joinpath("SKILL.md").write_text(
                    f"---\ndescription: {name} {'word ' * 80}\n---\n{name} full body with detailed instructions\n",
                    encoding="utf-8",
                )

            loader = SkillLoader([skills_dir])
            rendered = loader.prompt_index(max_description_chars=40, max_entries=2)

            self.assertIn("Skill index (summaries only; full instructions are lazy-loaded):", rendered)
            self.assertIn("Use `load_skill`", rendered)
            self.assertIn("- Alpha:", rendered)
            self.assertIn("- Beta:", rendered)
            self.assertIn("- Gamma", rendered)
            self.assertNotIn("- Gamma:", rendered)
            self.assertNotIn("more skill(s) omitted", rendered)
            self.assertNotIn("full body with detailed instructions", rendered)
            self.assertIn("Alpha full body with detailed instructions", loader.load("Alpha"))

    def test_prompt_index_default_description_limit_is_250_chars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "skills" / "Long"
            skill_dir.mkdir(parents=True)
            long_description = "x" * 280
            skill_dir.joinpath("SKILL.md").write_text(
                f"---\ndescription: {long_description}\n---\nfull body\n",
                encoding="utf-8",
            )

            loader = SkillLoader([root / "skills"])
            rendered = loader.prompt_index()

            self.assertEqual(DEFAULT_SKILL_PROMPT_DESCRIPTION_CHARS, 250)
            self.assertIn(f"- Long: {'x' * 247}...", rendered)

    def test_for_workspace_loads_project_claude_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            claude_skill_dir = root / ".claude" / "skills" / "gitnexus" / "gitnexus-exploring"
            claude_skill_dir.mkdir(parents=True)
            (claude_skill_dir / "SKILL.md").write_text(
                "---\ndescription: explore with graph\n---\nclaude skill body\n",
                encoding="utf-8",
            )

            loader = SkillLoader.for_workspace(root)

            self.assertIn("gitnexus-exploring", loader.names())
            self.assertIn("claude skill body", loader.load("gitnexus-exploring"))
            self.assertIn("- gitnexus-exploring [workspace-claude] - explore with graph", loader.render_listing())

    def test_workspace_somnia_skill_overrides_project_claude_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            claude_skill_dir = root / ".claude" / "skills" / "Review"
            somnia_skill_dir = root / ".open_somnia" / "skills" / "Review"
            claude_skill_dir.mkdir(parents=True)
            somnia_skill_dir.mkdir(parents=True)
            (claude_skill_dir / "SKILL.md").write_text(
                "---\ndescription: claude review\n---\nclaude body\n",
                encoding="utf-8",
            )
            (somnia_skill_dir / "SKILL.md").write_text(
                "---\ndescription: somnia review\n---\nsomnia body\n",
                encoding="utf-8",
            )

            loader = SkillLoader.for_workspace(root)

            self.assertEqual(loader.names().count("Review"), 1)
            self.assertIn("somnia body", loader.load("Review"))
            self.assertIn("- Review [workspace] - somnia review", loader.render_listing())


if __name__ == "__main__":
    unittest.main()
