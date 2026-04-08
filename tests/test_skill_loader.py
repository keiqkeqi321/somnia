from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openagent.skills.loader import SkillLoader


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
            skills_dir = root / ".openagent" / "skills"
            (skills_dir / "Review").mkdir(parents=True)
            (skills_dir / "Review" / "Skill.MD").write_text(
                "---\ndescription: review code\n---\nbody\n",
                encoding="utf-8",
            )

            loader = SkillLoader([skills_dir])
            rendered = loader.render_listing()

            self.assertIn("- Review [workspace] - review code", rendered)
            self.assertIn("use: /+Review", rendered)


if __name__ == "__main__":
    unittest.main()
