from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from open_somnia.runtime.project_instructions import ProjectInstructionsLoader


class ProjectInstructionsLoaderTests(unittest.TestCase):
    def test_load_prefers_agents_over_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "AGENTS.md").write_text("agents rules\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")

            rendered = ProjectInstructionsLoader(root).render()

            self.assertIn('source="AGENTS.md"', rendered)
            self.assertIn("agents rules", rendered)
            self.assertNotIn("claude rules", rendered)

    def test_load_uses_claude_when_agents_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")

            rendered = ProjectInstructionsLoader(root).render()

            self.assertIn('source="CLAUDE.md"', rendered)
            self.assertIn("claude rules", rendered)

    def test_render_returns_empty_when_no_instruction_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(ProjectInstructionsLoader(Path(tmpdir)).render(), "")

    def test_render_truncates_large_instruction_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "AGENTS.md").write_text("abcdef", encoding="utf-8")

            rendered = ProjectInstructionsLoader(root, max_chars=3).render()

            self.assertIn("abc", rendered)
            self.assertNotIn("abcdef", rendered)
            self.assertIn("truncated at 3 characters", rendered)


if __name__ == "__main__":
    unittest.main()
