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

    def test_render_loads_scoped_instructions_from_root_to_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nested = root / "desktop" / "ui"
            nested.mkdir(parents=True)
            (root / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
            (root / "desktop" / "AGENTS.md").write_text("desktop rules\n", encoding="utf-8")
            (nested / "CLAUDE.md").write_text("ui rules\n", encoding="utf-8")

            rendered = ProjectInstructionsLoader(root).render(paths=["desktop/ui/src/App.tsx"])

            self.assertIn('source="AGENTS.md" scope="."', rendered)
            self.assertIn('source="desktop/AGENTS.md" scope="desktop"', rendered)
            self.assertIn('source="desktop/ui/CLAUDE.md" scope="desktop/ui"', rendered)
            self.assertLess(rendered.index("root rules"), rendered.index("desktop rules"))
            self.assertLess(rendered.index("desktop rules"), rendered.index("ui rules"))
            self.assertIn("more specific directory scopes override broader scopes", rendered)

    def test_render_prefers_agents_over_claude_in_nested_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nested = root / "pkg"
            nested.mkdir()
            (nested / "AGENTS.md").write_text("agents nested\n", encoding="utf-8")
            (nested / "CLAUDE.md").write_text("claude nested\n", encoding="utf-8")

            rendered = ProjectInstructionsLoader(root).render(paths=["pkg/module.py"])

            self.assertIn("agents nested", rendered)
            self.assertNotIn("claude nested", rendered)

    def test_render_ignores_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = Path(tempfile.gettempdir()) / "outside.py"
            (root / "AGENTS.md").write_text("root rules\n", encoding="utf-8")

            rendered = ProjectInstructionsLoader(root).render(paths=[outside])

            self.assertIn("root rules", rendered)
            self.assertEqual(rendered.count("<project-instructions"), 1)


if __name__ == "__main__":
    unittest.main()
