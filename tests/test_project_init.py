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
            self.assertIn("Use tools such as tree", init_prompt.prompt)
            self.assertIn("Write AGENTS.md using write_file or edit_file", init_prompt.prompt)
            self.assertIn("verify the line count", init_prompt.prompt)
            self.assertIn("Demo package", init_prompt.prompt)
            self.assertIn("demo = demo.cli:main", init_prompt.prompt)
            self.assertIn("Do not paste the full AGENTS.md content", init_prompt.prompt)

    def test_build_project_init_prompt_includes_extra_user_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "main.py").write_text("print('hello')\n", encoding="utf-8")

            init_prompt = build_project_init_prompt(root, extra_prompt="重点分析 CLI 和测试命令")

            self.assertEqual(init_prompt.extra_prompt, "重点分析 CLI 和测试命令")
            self.assertIn("User extra instructions for this initialization:", init_prompt.prompt)
            self.assertIn("重点分析 CLI 和测试命令", init_prompt.prompt)
            self.assertIn("Do not copy them verbatim into AGENTS.md", init_prompt.prompt)

    def test_build_project_init_prompt_protects_gitnexus_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            block = "\n".join(
                [
                    "<!-- gitnexus:start -->",
                    "# GitNexus - Code Intelligence",
                    "Preserve this indexed guidance.",
                    "<!-- gitnexus:end -->",
                ]
            )
            (root / "AGENTS.md").write_text(f"before\n{block}\nafter\n", encoding="utf-8")
            (root / "main.py").write_text("print('hello')\n", encoding="utf-8")

            init_prompt = build_project_init_prompt(root, force=True)

            self.assertEqual(init_prompt.protected_gitnexus_block, block)
            self.assertIn("Protected indexed guidance detected", init_prompt.prompt)
            self.assertIn(block, init_prompt.prompt)
            self.assertIn("Preserve that block byte-for-byte", init_prompt.prompt)
            self.assertIn("even when overwrite is enabled", init_prompt.prompt)


if __name__ == "__main__":
    unittest.main()
