from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
            ctx = SimpleNamespace(
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(
                        workspace_root=root,
                        runtime=SimpleNamespace(max_tool_output_chars=50000),
                    )
                ),
                session=session,
            )
            result = write_file(ctx, {"path": "demo.txt", "content": "a\nb\n"})

        self.assertEqual(result["path"], "demo.txt")
        self.assertTrue(result["absolute_path"].endswith("demo.txt"))
        self.assertEqual(result["added_lines"], 2)
        self.assertEqual(result["removed_lines"], 0)
        self.assertEqual(len(session.pending_file_changes), 1)

    def test_edit_file_returns_diff_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "demo.txt"
            target.write_text("a\nb\n", encoding="utf-8")
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
            result = edit_file(ctx, {"path": "demo.txt", "old_text": "b\n", "new_text": "b\nc\n"})

        self.assertEqual(result["path"], "demo.txt")
        self.assertTrue(result["absolute_path"].endswith("demo.txt"))
        self.assertEqual(result["added_lines"], 1)
        self.assertEqual(result["removed_lines"], 0)
        self.assertEqual(len(session.pending_file_changes), 1)

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
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "Runtime" / "Core"
            target.mkdir(parents=True)
            (target / "PaperComponent.cs").write_text(
                "public class PaperComponent : MonoBehaviour {}\npublic void BuildMesh() {}\n",
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

            type_result = find_symbol(ctx, {"query": "PaperComp"})
            method_result = find_symbol(ctx, {"query": "BuildMesh"})

        self.assertIn("Runtime/Core/PaperComponent.cs:1:class PaperComponent", type_result)
        self.assertIn("Runtime/Core/PaperComponent.cs:2:method BuildMesh", method_result)

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

        self.assertEqual(result, "blocked by mode")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
