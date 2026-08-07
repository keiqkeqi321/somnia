# -*- coding: utf-8 -*-
""".gitignore 规则匹配与搜索工具集成测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_somnia.tools.filesystem import (
    glob_search,
    grep_search,
    list_ignored,
    tree_view,
)
from open_somnia.tools.gitignore import GitignoreMatcher


def _ctx(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                workspace_root=root,
                runtime=SimpleNamespace(max_tool_output_chars=50000),
            )
        ),
        session=None,
    )


class GitignoreMatcherTests(unittest.TestCase):
    def test_basename_pattern_matches_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(root, root)

            self.assertTrue(matcher.is_ignored(root / "a.log", is_dir=False))
            self.assertTrue(matcher.is_ignored(root / "sub" / "b.log", is_dir=False))
            self.assertFalse(matcher.is_ignored(root / "a.txt", is_dir=False))

    def test_dir_only_pattern_matches_dirs_not_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(root, root)

            self.assertTrue(matcher.is_ignored(root / "build", is_dir=True))
            self.assertTrue(matcher.is_ignored(root / "sub" / "build", is_dir=True))
            self.assertFalse(matcher.is_ignored(root / "build", is_dir=False))

    def test_anchored_pattern_only_matches_at_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("/rooted.txt\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(root, root)

            self.assertTrue(matcher.is_ignored(root / "rooted.txt", is_dir=False))
            self.assertFalse(matcher.is_ignored(root / "sub" / "rooted.txt", is_dir=False))

    def test_negation_reincludes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(root, root)

            self.assertTrue(matcher.is_ignored(root / "a.log", is_dir=False))
            self.assertFalse(matcher.is_ignored(root / "keep.log", is_dir=False))

    def test_nested_gitignore_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sub = root / "sub"
            sub.mkdir()
            (root / ".gitignore").write_text("*.tmp.txt\n", encoding="utf-8")
            (sub / ".gitignore").write_text("!allow.tmp.txt\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(root, root)

            self.assertTrue(matcher.is_ignored(root / "x.tmp.txt", is_dir=False))
            self.assertTrue(matcher.is_ignored(sub / "x.tmp.txt", is_dir=False))
            self.assertFalse(matcher.is_ignored(sub / "allow.tmp.txt", is_dir=False))

    def test_doublestar_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("**/generated/**.cpp\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(root, root)

            self.assertTrue(matcher.is_ignored(root / "a" / "generated" / "x.cpp", is_dir=False))
            self.assertFalse(matcher.is_ignored(root / "a" / "src" / "x.cpp", is_dir=False))

    def test_no_gitignore_ignores_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            matcher = GitignoreMatcher.for_walk(root, root)

            self.assertFalse(matcher.is_ignored(root / "a.log", is_dir=False))
            self.assertFalse(matcher.is_ignored(root / "build", is_dir=True))

    def test_base_outside_workspace_uses_its_own_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            workspace_root = Path(workspace_tmp)
            outside = Path(outside_tmp)
            (outside / ".gitignore").write_text("*.log\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(workspace_root, outside)

            self.assertTrue(matcher.is_ignored(outside / "a.log", is_dir=False))
            self.assertFalse(matcher.is_ignored(outside / "a.txt", is_dir=False))

    def test_check_reports_deciding_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("# comment\n\n*.log\n", encoding="utf-8")
            matcher = GitignoreMatcher.for_walk(root, root)

            ignored, source = matcher.check(root / "a.log", is_dir=False)

            self.assertTrue(ignored)
            self.assertIsNotNone(source)
            label, line_number, pattern_text = source
            self.assertEqual(label, ".gitignore")
            self.assertEqual(line_number, 3)
            self.assertEqual(pattern_text, "*.log")


class SearchToolsRespectGitignoreTests(unittest.TestCase):
    def test_grep_search_skips_ignored_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("ignored_dir/\n*.gen.txt\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("needle\n", encoding="utf-8")
            (root / "ignored_dir").mkdir()
            (root / "ignored_dir" / "hidden.py").write_text("needle\n", encoding="utf-8")
            (root / "src" / "a.gen.txt").write_text("needle\n", encoding="utf-8")

            result = grep_search(_ctx(root), {"pattern": "needle"})

        self.assertEqual(result, "src/app.py:1:needle")

    def test_grep_search_honors_negation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
            (root / "keep.log").write_text("needle\n", encoding="utf-8")
            (root / "skip.log").write_text("needle\n", encoding="utf-8")

            result = grep_search(_ctx(root), {"pattern": "needle"})

        self.assertEqual(result, "keep.log:1:needle")

    def test_grep_search_explicit_ignored_file_is_still_searched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            (root / "debug.log").write_text("needle\n", encoding="utf-8")

            result = grep_search(_ctx(root), {"path": "debug.log", "pattern": "needle"})

        self.assertEqual(result, "debug.log:1:needle")

    def test_glob_search_skips_ignored_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("gen/\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
            (root / "gen").mkdir()
            (root / "gen" / "out.py").write_text("x = 2\n", encoding="utf-8")

            result = glob_search(_ctx(root), {"pattern": "**/*.py"})

        self.assertEqual(result, "src/app.py")

    def test_glob_search_non_recursive_skips_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            (root / "debug.log").write_text("x = 2\n", encoding="utf-8")

            result = glob_search(_ctx(root), {"pattern": "*", "recursive": False})

        self.assertNotIn("debug.log", result)
        self.assertIn("app.py", result)

    def test_tree_view_skips_ignored_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("proj.ios/\n", encoding="utf-8")
            (root / "proj.ios").mkdir()
            (root / "proj.android").mkdir()

            result = tree_view(_ctx(root), {"path": ".", "depth": 1})

        self.assertIn("proj.android/", result)
        self.assertNotIn("proj.ios", result)


class ListIgnoredToolTests(unittest.TestCase):
    def test_lists_ignored_dir_once_with_rule_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("proj.android/\n*.log\n", encoding="utf-8")
            (root / "proj.android").mkdir()
            (root / "proj.android" / "many.txt").write_text("x\n", encoding="utf-8")
            (root / "debug.log").write_text("x\n", encoding="utf-8")
            (root / "keep.txt").write_text("x\n", encoding="utf-8")

            result = list_ignored(_ctx(root), {})

        self.assertIn("proj.android/  [.gitignore:1: `proj.android/`]", result)
        self.assertIn("debug.log  [.gitignore:2: `*.log`]", result)
        self.assertNotIn("many.txt", result)
        self.assertNotIn("keep.txt", result)
        self.assertIn("2 by .gitignore rules", result)

    def test_marks_builtin_ignored_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")
            (root / "src").mkdir()

            result = list_ignored(_ctx(root), {})

        self.assertIn("node_modules/  [builtin ignore list]", result)
        self.assertNotIn("pkg.js", result)
        self.assertIn("1 by builtin list", result)

    def test_include_builtin_false_hides_builtin_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "node_modules").mkdir()

            result = list_ignored(_ctx(root), {"include_builtin": False})

        self.assertEqual(result, f"(no ignored paths under .)")

    def test_no_ignored_paths_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

            result = list_ignored(_ctx(root), {})

        self.assertEqual(result, "(no ignored paths under .)")

    def test_limit_truncates_entries_but_keeps_total_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            for index in range(5):
                (root / f"{index}.log").write_text("x\n", encoding="utf-8")

            result = list_ignored(_ctx(root), {"limit": 2})

        self.assertIn("5 ignored path(s)", result)
        self.assertIn("showing first 2", result)
        self.assertEqual(result.count("  [.gitignore:1: `*.log`]"), 2)

    def test_nested_gitignore_source_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sub = root / "sub"
            sub.mkdir()
            (sub / ".gitignore").write_text("out/\n", encoding="utf-8")
            (sub / "out").mkdir()

            result = list_ignored(_ctx(root), {})

        self.assertIn("sub/out/  [sub/.gitignore:1: `out/`]", result)


if __name__ == "__main__":
    unittest.main()
