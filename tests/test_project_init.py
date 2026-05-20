from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from open_somnia.runtime.project_init import build_project_init_prompt, init_line_limit


class ProjectInitTests(unittest.TestCase):
    def test_line_limit_scales_with_code_file_count(self) -> None:
        self.assertEqual(init_line_limit(0), 60)
        self.assertEqual(init_line_limit(80), 60)
        self.assertEqual(init_line_limit(81), 80)
        self.assertEqual(init_line_limit(300), 80)
        self.assertEqual(init_line_limit(301), 120)
        self.assertEqual(init_line_limit(1201), 160)
        self.assertEqual(init_line_limit(5001), 200)

    def test_build_project_init_prompt_requires_real_inspection_and_file_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pyproject.toml").write_text(
                "[project]\nname = \"demo\"\ndescription = \"Demo package\"\n[project.scripts]\ndemo = \"demo.cli:main\"\n",
                encoding="utf-8",
            )
            package_dir = root / "demo"
            tests_dir = root / "tests"
            package_dir.mkdir()
            tests_dir.mkdir()
            (package_dir / "cli.py").write_text("def main():\n    pass\n", encoding="utf-8")
            (tests_dir / "test_cli.py").write_text("import unittest\n", encoding="utf-8")

            init_prompt = build_project_init_prompt(root)

            self.assertEqual(init_prompt.target_path, root / "AGENTS.md")
            self.assertEqual(init_prompt.line_limit, 60)
            self.assertIn("You must run a real repository inspection loop", init_prompt.prompt)
            self.assertIn("Use tools such as project_scan", init_prompt.prompt)
            self.assertIn("Write AGENTS.md using write_file or edit_file", init_prompt.prompt)
            self.assertIn("verify the line count", init_prompt.prompt)
            self.assertIn("Demo package", init_prompt.prompt)
            self.assertIn("demo = demo.cli:main", init_prompt.prompt)
            self.assertIn("Do not paste the full AGENTS.md content", init_prompt.prompt)


if __name__ == "__main__":
    unittest.main()
