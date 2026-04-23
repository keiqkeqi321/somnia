from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.tools.filesystem import (
    edit_file,
    find_symbol,
    glob_search,
    grep_search,
    project_scan,
    read_file,
    safe_path,
    tree_view,
    write_file,
)
from open_somnia.tools.registry import ToolDefinition, ToolRegistry


class FilesystemToolTests(unittest.TestCase):
    def test_tool_registry_reraises_turn_interrupted(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="probe",
                description="Interruptible test tool.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: (_ for _ in ()).throw(TurnInterrupted("Interrupted by user.")),
            )
        )

        with self.assertRaises(TurnInterrupted):
            registry.execute(SimpleNamespace(runtime=None), "probe", {})

    def test_grep_search_raises_turn_interrupted_when_context_requests_stop(self) -> None:
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=Path.cwd(),
                    runtime=SimpleNamespace(max_tool_output_chars=50000),
                )
            ),
            should_interrupt=lambda: True,
        )

        with self.assertRaises(TurnInterrupted):
            grep_search(ctx, {"pattern": "beta", "glob": "*.py"})

    def test_safe_path_normalizes_workspace_root_before_boundary_check(self) -> None:
        class _FakeResolvedPath:
            def __init__(self, value: str) -> None:
                self.value = value

            def resolve(self):
                return self

            def __truediv__(self, relative: str):
                return _FakeJoinedPath(self.value, relative)

            def is_relative_to(self, other) -> bool:
                base = getattr(other, "value", getattr(other, "raw_value", str(other))).rstrip("/\\")
                candidate = self.value.rstrip("/\\")
                return candidate == base or candidate.startswith(base + "\\")

        class _FakeJoinedPath:
            def __init__(self, base_value: str, relative: str) -> None:
                self.base_value = base_value.rstrip("/\\")
                self.relative = relative

            def resolve(self):
                return _FakeResolvedPath(f"{self.base_value}\\{self.relative}")

        class _FakeWorkspaceRoot:
            def __init__(self, raw_value: str, resolved_value: str) -> None:
                self.raw_value = raw_value
                self.resolved_value = resolved_value

            def resolve(self):
                return _FakeResolvedPath(self.resolved_value)

            def __truediv__(self, relative: str):
                return _FakeJoinedPath(self.resolved_value, relative)

            def __str__(self) -> str:
                return self.raw_value

        resolved = safe_path(
            _FakeWorkspaceRoot(
                raw_value=r"C:\Users\KEQIKE~1\AppData\Local\Temp\tmpabcd",
                resolved_value=r"C:\Users\keqikeqi321\AppData\Local\Temp\tmpabcd",
            ),
            "demo.txt",
        )

        self.assertEqual(resolved.value, r"C:\Users\keqikeqi321\AppData\Local\Temp\tmpabcd\demo.txt")

    def test_write_file_returns_diff_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            session = SimpleNamespace(pending_file_changes=[])
            active_files: list[dict[str, str]] = []
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    ),
                    note_active_file=lambda **kwargs: active_files.append(kwargs),
                ),
                session=session,
            )
            result = write_file(ctx, {"path": "demo.txt", "content": "a\nb\n"})

        self.assertEqual(result["path"], "demo.txt")
        self.assertTrue(result["absolute_path"].endswith("demo.txt"))
        self.assertEqual(result["added_lines"], 2)
        self.assertEqual(result["removed_lines"], 0)
        self.assertIn("1: a", result["updated_content_snippet"])
        self.assertEqual(len(session.pending_file_changes), 1)
        self.assertEqual(active_files[0]["path"], "demo.txt")
        self.assertEqual(active_files[0]["source"], "write_file")

    def test_edit_file_requires_edits_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.txt"
            target.write_text("a\nb\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=SimpleNamespace(pending_file_changes=[]),
            )

            result = edit_file(ctx, {"path": "demo.txt", "old_text": "b\n", "new_text": "b\nc\n"})

        self.assertEqual(
            result,
            "Error: edits must be a non-empty list. Wrap even one replacement as edits=[{old_text, new_text}].",
        )

    def test_edit_file_returns_diff_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.txt"
            target.write_text("a\nb\n", encoding="utf-8")
            session = SimpleNamespace(pending_file_changes=[])
            active_files: list[dict[str, str]] = []
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    ),
                    note_active_file=lambda **kwargs: active_files.append(kwargs),
                ),
                session=session,
            )
            result = edit_file(
                ctx,
                {
                    "path": "demo.txt",
                    "edits": [{"old_text": "b\n", "new_text": "b\nc\n"}],
                },
            )

        self.assertEqual(result["path"], "demo.txt")
        self.assertTrue(result["absolute_path"].endswith("demo.txt"))
        self.assertEqual(result["added_lines"], 1)
        self.assertEqual(result["removed_lines"], 0)
        self.assertEqual(result["applied_edits"], 1)
        self.assertIn("2: b", result["updated_content_snippet"])
        self.assertIn("3: c", result["updated_content_snippet"])
        self.assertEqual(len(session.pending_file_changes), 1)
        self.assertEqual(active_files[0]["path"], "demo.txt")
        self.assertEqual(active_files[0]["source"], "edit_file")

    def test_edit_file_accepts_stringified_edits_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.txt"
            target.write_text("alpha\nbeta\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=SimpleNamespace(pending_file_changes=[]),
            )

            result = edit_file(
                ctx,
                {
                    "path": "demo.txt",
                    "edits": '[{"old_text":"beta","new_text":"beta updated"}]',
                },
            )
            final_content = target.read_text(encoding="utf-8")

        self.assertEqual(result["applied_edits"], 1)
        self.assertEqual(final_content, "alpha\nbeta updated\n")

    def test_edit_file_supports_multiple_replacements_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.txt"
            target.write_text("alpha\nbeta\nrender old\n", encoding="utf-8")
            session = SimpleNamespace(pending_file_changes=[])
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=session,
            )

            result = edit_file(
                ctx,
                {
                    "path": "demo.txt",
                    "edits": [
                        {"old_text": "alpha", "new_text": "alpha updated"},
                        {"old_text": "render old", "new_text": "render new"},
                    ],
                },
            )
            final_content = (root / "demo.txt").read_text(encoding="utf-8")

        self.assertEqual(result["applied_edits"], 2)
        self.assertIn("1: alpha updated", result["updated_content_snippet"])
        self.assertIn("3: render new", result["updated_content_snippet"])
        self.assertEqual(final_content, "alpha updated\nbeta\nrender new\n")

    def test_edit_file_accepts_per_edit_path_without_top_level_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "frontend" / "src" / "ChatSidebar.tsx"
            target.parent.mkdir(parents=True)
            target.write_text("alpha\nbeta\n", encoding="utf-8")
            session = SimpleNamespace(pending_file_changes=[])
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=session,
            )

            result = edit_file(
                ctx,
                {
                    "edits": [
                        {
                            "path": "frontend/src/ChatSidebar.tsx",
                            "old_text": "beta",
                            "new_text": "beta updated",
                        }
                    ]
                },
            )
            final_content = target.read_text(encoding="utf-8")

        self.assertEqual(result["path"], "frontend/src/ChatSidebar.tsx")
        self.assertEqual(result["applied_edits"], 1)
        self.assertEqual(final_content, "alpha\nbeta updated\n")

    def test_edit_file_accepts_absolute_workspace_path_in_edit_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "frontend" / "src" / "ChatSidebar.tsx"
            target.parent.mkdir(parents=True)
            target.write_text("one\ntwo\n", encoding="utf-8")
            session = SimpleNamespace(pending_file_changes=[])
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=session,
            )

            result = edit_file(
                ctx,
                {
                    "edits": [
                        {
                            "path": str(target),
                            "old_text": "two",
                            "new_text": "two updated",
                        }
                    ]
                },
            )
            final_content = target.read_text(encoding="utf-8")

        self.assertEqual(result["path"], str(target))
        self.assertEqual(result["applied_edits"], 1)
        self.assertEqual(final_content, "one\ntwo updated\n")

    def test_read_file_updates_active_file_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.txt"
            target.write_text("line 1\nline 2\n", encoding="utf-8")
            active_files: list[dict[str, str]] = []
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    ),
                    note_active_file=lambda **kwargs: active_files.append(kwargs),
                ),
                session=None,
            )

            result = read_file(ctx, {"path": "demo.txt"})

        self.assertEqual(result, "line 1\nline 2")
        self.assertEqual(active_files[0]["path"], "demo.txt")
        self.assertEqual(active_files[0]["source"], "read_file")
        self.assertEqual(active_files[0]["content"], "line 1\nline 2\n")

    def test_read_file_supports_start_line_and_limit(self) -> None:
        root = Path.cwd() / ".tmp-tests" / self._testMethodName
        root.mkdir(parents=True, exist_ok=True)
        target = root / "demo.txt"
        target.write_text("line 1\nline 2\nline 3\nline 4\nline 5\nline 6\n", encoding="utf-8")
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=root,
                    runtime=SimpleNamespace(max_tool_output_chars=50000),
                )
            ),
            session=None,
        )

        result = read_file(ctx, {"path": "demo.txt", "start_line": 3, "limit": 2})

        self.assertEqual(
            result,
            "... (2 lines omitted before line 3)\nline 3\nline 4\n... (2 more lines after line 4)",
        )

    def test_read_file_supports_end_line(self) -> None:
        root = Path.cwd() / ".tmp-tests" / self._testMethodName
        root.mkdir(parents=True, exist_ok=True)
        target = root / "demo.txt"
        target.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8")
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=root,
                    runtime=SimpleNamespace(max_tool_output_chars=50000),
                )
            ),
            session=None,
        )

        result = read_file(ctx, {"path": "demo.txt", "start_line": 2, "end_line": 4})

        self.assertEqual(
            result,
            "... (1 lines omitted before line 2)\nline 2\nline 3\nline 4\n... (1 more lines after line 4)",
        )

    def test_read_file_returns_explicit_truncation_marker_when_output_hits_char_limit(self) -> None:
        root = Path.cwd() / ".tmp-tests" / self._testMethodName
        root.mkdir(parents=True, exist_ok=True)
        target = root / "demo.txt"
        target.write_text(("0123456789\n" * 30), encoding="utf-8")
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=root,
                    runtime=SimpleNamespace(max_tool_output_chars=120),
                )
            ),
            session=None,
        )

        result = read_file(ctx, {"path": "demo.txt"})

        self.assertLessEqual(len(result), 120)
        self.assertIn("[read_file output truncated at 120 chars;", result)
        self.assertIn("use start_line/end_line", result)

    def test_glob_search_returns_matching_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = glob_search(ctx, {"pattern": "*.py", "recursive": True})

        self.assertEqual(result, "src/app.py")

    def test_glob_search_supports_brace_expansion_for_multiple_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frontend" / "src" / "api").mkdir(parents=True)
            (root / "frontend" / "src" / "App.tsx").write_text("export default function App() {}\n", encoding="utf-8")
            (root / "frontend" / "src" / "api" / "conversations.ts").write_text("export const getMessages = () => {}\n", encoding="utf-8")
            (root / "frontend" / "src" / "styles.css").write_text("body {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = glob_search(ctx, {"path": "frontend/src", "pattern": "*.{ts,tsx}", "recursive": True})

        self.assertEqual(
            set(result.splitlines()),
            {
                "frontend/src/App.tsx",
                "frontend/src/api/conversations.ts",
            },
        )

    def test_glob_search_accepts_workspace_prefixed_pattern_under_narrower_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frontend" / "src" / "api").mkdir(parents=True)
            (root / "frontend" / "src" / "api" / "conversations.ts").write_text("export const getMessages = () => {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = glob_search(
                ctx,
                {
                    "path": "frontend/src",
                    "pattern": "frontend/src/api/*.{ts,tsx}",
                    "recursive": False,
                    "match": "files",
                },
            )

        self.assertEqual(result, "frontend/src/api/conversations.ts")

    def test_tree_view_renders_shallow_project_map_and_skips_noise_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Runtime" / "Core").mkdir(parents=True)
            (root / "Library").mkdir()
            (root / "Runtime" / "Core" / "PaperComponent.cs").write_text("class PaperComponent {}\n", encoding="utf-8")
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = tree_view(ctx, {"path": ".", "depth": 2})

        self.assertIn("./", result)
        self.assertIn("Runtime/", result)
        self.assertIn("README.md", result)
        self.assertNotIn("Library/", result)

    def test_glob_search_allows_explicit_subdirectory_patterns_without_recursive_walk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "OpenAgent" / "scripts").mkdir(parents=True)
            (root / "OpenAgent" / "README.md").write_text("hello\n", encoding="utf-8")
            (root / "OpenAgent" / "scripts" / "install.sh").write_text("echo ok\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = glob_search(
                ctx,
                {
                    "pattern": "OpenAgent/scripts/*",
                    "path": ".",
                    "recursive": False,
                    "match": "files",
                },
            )

        self.assertEqual(result, "OpenAgent/scripts/install.sh")

    def test_glob_search_allows_hidden_file_patterns_inside_explicit_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "OpenAgent").mkdir()
            (root / "OpenAgent" / ".gitignore").write_text("dist/\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = glob_search(
                ctx,
                {
                    "pattern": "OpenAgent/.*",
                    "path": ".",
                    "recursive": False,
                    "match": "files",
                },
            )

        self.assertEqual(result, "OpenAgent/.gitignore")

    def test_glob_search_no_matches_explains_recursive_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src" / "nested").mkdir(parents=True)
            (root / "src" / "nested" / "app.py").write_text("print('hi')\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = glob_search(
                ctx,
                {
                    "pattern": "*.py",
                    "path": "src",
                    "recursive": False,
                    "match": "files",
                },
            )

        self.assertIn("(no matches)", result)
        self.assertIn("path: src", result)
        self.assertIn("pattern: *.py", result)
        self.assertIn("recursive: false", result)
        self.assertIn("Set `recursive=true` to walk deeper", result)

    def test_glob_search_no_matches_explains_match_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "OpenAgent" / "scripts").mkdir(parents=True)
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = glob_search(
                ctx,
                {
                    "pattern": "OpenAgent/*",
                    "path": ".",
                    "recursive": False,
                    "match": "files",
                },
            )

        self.assertIn("(no matches)", result)
        self.assertIn("pattern: OpenAgent/*", result)
        self.assertIn("match=files", result)
        self.assertIn("Try `match=dirs` or `match=all`", result)

    def test_read_file_falls_back_for_gbk_encoded_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.cs"
            target.write_bytes("第一行\n第二行\n".encode("gb18030"))
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = read_file(ctx, {"path": "demo.cs"})

        self.assertEqual(result, "第一行\n第二行")

    def test_find_symbol_locates_csharp_types_and_methods_by_substring(self) -> None:
        root = Path.cwd()
        candidate = root / "Runtime" / "Core" / "PaperComponent.cs"
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=root,
                    runtime=SimpleNamespace(max_tool_output_chars=50000),
                )
            ),
            session=None,
        )

        with patch("open_somnia.tools.filesystem._filtered_walk", return_value=[(candidate.parent, [], [candidate.name])]), patch(
            "open_somnia.tools.filesystem._read_text_with_fallback",
            return_value="public class PaperComponent : MonoBehaviour {}\npublic void BuildMesh() {}\n",
        ):
            type_result = find_symbol(ctx, {"query": "PaperComp"})
            method_result = find_symbol(ctx, {"query": "BuildMesh"})

        self.assertIn("Runtime/Core/PaperComponent.cs:1:class PaperComponent", type_result)
        self.assertIn("Runtime/Core/PaperComponent.cs:2:method BuildMesh", method_result)

    def test_find_symbol_supports_pipe_separated_broad_search_terms(self) -> None:
        root = Path.cwd()
        candidate = root / "Runtime" / "Core" / "PaperComponent.cs"
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=root,
                    runtime=SimpleNamespace(max_tool_output_chars=50000),
                )
            ),
            session=None,
        )

        with patch("open_somnia.tools.filesystem._filtered_walk", return_value=[(candidate.parent, [], [candidate.name])]), patch(
            "open_somnia.tools.filesystem._read_text_with_fallback",
            return_value="public class PaperComponent : MonoBehaviour {}\npublic void BuildMesh() {}\n",
        ):
            result = find_symbol(ctx, {"query": "PaperComp|BuildMesh"})

        self.assertIn("Runtime/Core/PaperComponent.cs:1:class PaperComponent", result)
        self.assertIn("Runtime/Core/PaperComponent.cs:2:method BuildMesh", result)

    def test_find_symbol_accepts_file_path_and_scans_only_that_file(self) -> None:
        root = Path.cwd()
        candidate = root / "open_somnia" / "tools" / "filesystem.py"
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=root,
                    runtime=SimpleNamespace(max_tool_output_chars=50000),
                )
            ),
            session=None,
        )

        with patch("open_somnia.tools.filesystem._filtered_walk") as mock_walk, patch(
            "open_somnia.tools.filesystem._read_text_with_fallback",
            return_value="def _create_executor_for_agent():\n    pass\n\ndef other_helper():\n    pass\n",
        ):
            result = find_symbol(
                ctx,
                {"query": "_create_executor_for_agent", "path": "open_somnia/tools/filesystem.py"},
            )

        mock_walk.assert_not_called()
        self.assertEqual(result, "open_somnia/tools/filesystem.py:1:function _create_executor_for_agent")

    def test_find_symbol_rejects_more_than_ten_pipe_separated_terms(self) -> None:
        root = Path.cwd()
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_root=root,
                    runtime=SimpleNamespace(max_tool_output_chars=50000),
                )
            ),
            session=None,
        )

        result = find_symbol(ctx, {"query": "a|b|c|d|e|f|g|h|i|j|k"})

        self.assertEqual(result, "Error: query supports at most 10 terms separated by '|'.")

    def test_project_scan_summarizes_guidance_source_roots_and_languages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Assets" / "Scripts").mkdir(parents=True)
            (root / "Packages").mkdir()
            (root / "Assets" / "Scripts" / "PaperComponent.cs").write_text("public class PaperComponent {}\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            (root / "README.md").write_text("intro\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = project_scan(ctx, {"path": ".", "depth": 2})

        self.assertIn("Guidance files:", result)
        self.assertIn("AGENTS.md", result)
        self.assertIn("Manifests:", result)
        self.assertIn("pyproject.toml", result)
        self.assertIn("Likely source roots:", result)
        self.assertIn("Assets/", result)
        self.assertIn("Languages/files:", result)
        self.assertIn(".cs: 1", result)

    def test_read_file_auto_resolves_unique_missing_filename_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "Runtime" / "UI"
            target.mkdir(parents=True)
            (target / "OrigamiLevelUI.cs").write_text("class OrigamiLevelUI {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = read_file(ctx, {"path": "Runtime/OrigamiLevelUI.cs"})

        self.assertIn("[auto-resolved path]", result)
        self.assertIn("using Runtime/UI/OrigamiLevelUI.cs", result)
        self.assertIn("class OrigamiLevelUI {}", result)

    def test_read_file_returns_candidate_list_when_missing_filename_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Runtime" / "UI").mkdir(parents=True)
            (root / "Runtime" / "Legacy").mkdir(parents=True)
            (root / "Runtime" / "UI" / "OrigamiLevelUI.cs").write_text("runtime\n", encoding="utf-8")
            (root / "Runtime" / "Legacy" / "OrigamiLevelUI.cs").write_text("legacy\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = read_file(ctx, {"path": "Runtime/OrigamiLevelUI.cs"})

        self.assertIn("Error: File not found: Runtime/OrigamiLevelUI.cs", result)
        self.assertIn("Closest matches:", result)
        self.assertIn("Runtime/UI/OrigamiLevelUI.cs", result)
        self.assertIn("Runtime/Legacy/OrigamiLevelUI.cs", result)

    def test_read_file_returns_similar_filename_candidates_when_no_exact_match_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Runtime").mkdir(parents=True)
            (root / "Runtime" / "OrigamiLevelConfig.cs").write_text("level\n", encoding="utf-8")
            (root / "Runtime" / "OrigamiPaperConfig.cs").write_text("paper\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = read_file(ctx, {"path": "Runtime/OrigamiGameConfig.cs"})

        self.assertIn("Error: File not found: Runtime/OrigamiGameConfig.cs", result)
        self.assertIn("Similar filenames:", result)
        self.assertIn("Runtime/OrigamiLevelConfig.cs", result)
        self.assertIn("Runtime/OrigamiPaperConfig.cs", result)

    def test_grep_search_returns_matching_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("alpha\nbeta\n", encoding="utf-8")
            (root / "README.md").write_text("beta docs\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": "beta", "glob": "*.py"})

        self.assertEqual(result, "src/app.py:2:beta")

    def test_grep_search_supports_brace_expansion_and_base_relative_glob(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frontend" / "src" / "api").mkdir(parents=True)
            (root / "frontend" / "src" / "api" / "conversations.ts").write_text(
                "export const getMessages = () => '/conversations/messages'\n",
                encoding="utf-8",
            )
            (root / "frontend" / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(
                ctx,
                {
                    "path": "frontend/src",
                    "pattern": "getMessages",
                    "glob": "api/*.{ts,tsx}",
                },
            )

        self.assertEqual(result, "frontend/src/api/conversations.ts:1:export const getMessages = () => '/conversations/messages'")

    def test_grep_search_accepts_workspace_prefixed_glob_under_narrower_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frontend" / "src" / "api").mkdir(parents=True)
            (root / "frontend" / "src" / "api" / "conversations.ts").write_text(
                "export const getMessages = () => '/conversations/messages'\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(
                ctx,
                {
                    "path": "frontend/src",
                    "pattern": "getMessages",
                    "glob": "frontend/src/api/*.{ts,tsx}",
                },
            )

        self.assertEqual(result, "frontend/src/api/conversations.ts:1:export const getMessages = () => '/conversations/messages'")

    def test_grep_search_accepts_single_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("alpha\nbeta\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": "beta", "path": "src/app.py"})

        self.assertEqual(result, "src/app.py:2:beta")

    def test_grep_search_single_file_path_ignores_glob_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("alpha\nbeta\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": "beta", "path": "src/app.py", "glob": "*.md"})

        self.assertEqual(result, "src/app.py:2:beta")

    def test_grep_search_reads_gbk_encoded_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.cs"
            target.write_bytes("装饰器\n对象池\n".encode("gb18030"))
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": "对象池", "glob": "*.cs"})

        self.assertEqual(result, "demo.cs:2:对象池")

    def test_grep_search_auto_enables_regex_for_alternation_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("auto_compact\ncompact_manager\n_messages_for_payload\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": "auto_compact|compact_manager|_messages_for_payload", "glob": "*.py"})

        self.assertEqual(
            result,
            "src/app.py:1:auto_compact\nsrc/app.py:2:compact_manager\nsrc/app.py:3:_messages_for_payload",
        )

    def test_grep_search_auto_enables_regex_for_word_boundary_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("error\nterror\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": r"\berror\b", "glob": "*.py"})

        self.assertEqual(result, "src/app.py:1:error")

    def test_grep_search_keeps_plain_text_quantifier_characters_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("hell\nhello\nhello?\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": "hello?", "glob": "*.py"})

        self.assertEqual(result, "src/app.py:3:hello?")

    def test_grep_search_keeps_windows_like_paths_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text(r"path\bin\file" + "\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": r"path\bin\file", "glob": "*.py"})

        self.assertEqual(result, r"src/app.py:1:path\bin\file")

    def test_grep_search_explicit_false_keeps_literal_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("literal auto_compact|compact_manager\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(
                ctx,
                {
                    "pattern": "auto_compact|compact_manager",
                    "glob": "*.py",
                    "use_regex": False,
                },
            )

        self.assertEqual(result, "src/app.py:1:literal auto_compact|compact_manager")

    def test_grep_search_invalid_explicit_regex_returns_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("beta\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=None,
            )

            result = grep_search(ctx, {"pattern": "(", "glob": "*.py", "use_regex": True})

        self.assertIn("Error: invalid regex pattern:", result)

    def test_tool_registry_applies_execution_mode_guard_before_write_handler(self) -> None:
        registry = ToolRegistry()
        called: list[dict[str, str]] = []
        registry.register(
            ToolDefinition(
                name="write_file",
                description="Write content to a file.",
                input_schema={"type": "object", "properties": {}},
                handler=lambda ctx, payload: called.append(payload) or {"status": "ok"},
            )
        )
        ctx = SimpleNamespace(
            runtime=SimpleNamespace(authorize_tool_call=lambda name, payload, ctx=None: "blocked by mode"),
            session=None,
        )

        result = registry.execute(ctx, "write_file", {"path": "demo.txt"})

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "tool_access_blocked")
        self.assertEqual(result["tool_name"], "write_file")
        self.assertEqual(result["message"], "blocked by mode")
        self.assertEqual(called, [])

    def test_tool_registry_returns_structured_missing_required_params(self) -> None:
        registry = ToolRegistry()
        called: list[dict[str, str]] = []
        registry.register(
            ToolDefinition(
                name="write_file",
                description="Write content to a file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                handler=lambda ctx, payload: called.append(payload) or {"status": "ok"},
            )
        )
        ctx = SimpleNamespace(runtime=SimpleNamespace(), session=None)

        result = registry.execute(ctx, "write_file", {"path": "demo.txt"})

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "missing_required_params")
        self.assertEqual(result["tool_name"], "write_file")
        self.assertEqual(result["missing_params"], ["content"])
        self.assertEqual(result["repair_hint"], {"required": ["path", "content"]})
        self.assertEqual(called, [])

    def test_tool_registry_classifies_edit_file_text_miss_as_content_not_found(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="edit_file",
                description="Replace exact text in one or more files.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                },
                                "required": ["old_text", "new_text"],
                            },
                        },
                    },
                    "required": ["edits"],
                },
                handler=edit_file,
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "backend" / "libicrab" / "api" / "v1" / "conversations.py"
            target.parent.mkdir(parents=True)
            target.write_text("return ApiResponse(data=messages)\n", encoding="utf-8")
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=SimpleNamespace(pending_file_changes=[]),
            )

            result = registry.execute(
                ctx,
                "edit_file",
                {
                    "path": "backend/libicrab/api/v1/conversations.py",
                    "edits": [
                        {
                            "old_text": "missing old text",
                            "new_text": "replacement",
                        }
                    ],
                },
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "content_not_found")
        self.assertEqual(result["tool_name"], "edit_file")
        self.assertIn("Text not found for edits[1]", result["message"])

    def test_tool_registry_keeps_real_missing_file_as_file_not_found(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="edit_file",
                description="Replace exact text in one or more files.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                },
                                "required": ["old_text", "new_text"],
                            },
                        },
                    },
                    "required": ["edits"],
                },
                handler=edit_file,
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=SimpleNamespace(pending_file_changes=[]),
            )

            result = registry.execute(
                ctx,
                "edit_file",
                {
                    "path": "backend/libicrab/api/v1/conversations.py",
                    "edits": [
                        {
                            "old_text": "missing old text",
                            "new_text": "replacement",
                        }
                    ],
                },
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "file_not_found")
        self.assertEqual(result["tool_name"], "edit_file")


if __name__ == "__main__":
    unittest.main()
