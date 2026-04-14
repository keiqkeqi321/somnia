"""文件系统工具模块.

提供文件读写、编辑等操作的工具函数。
"""

from __future__ import annotations

import difflib
import fnmatch
from collections import Counter
import os
from pathlib import Path
import re
from typing import Any

from open_somnia.tools.registry import ToolDefinition

READ_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "cp936")
EXPLORATION_IGNORED_DIR_NAMES = {
    ".git",
    ".open_somnia",
    "__pycache__",
    ".venv",
    "node_modules",
    "Library",
    "Temp",
    "Logs",
    "obj",
    "bin",
    "dist",
}
CODE_FILE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
}
GUIDANCE_FILENAMES = {"AGENTS.md", "CLAUDE.md", "README.md", "README"}
MANIFEST_FILENAMES = {
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "ProjectSettings.asset",
    "Packages/manifest.json",
}
ENTRY_FILE_NAMES = {
    "app.py",
    "main.py",
    "Program.cs",
    "Main.cs",
    "cli.py",
    "index.ts",
    "index.tsx",
    "main.ts",
    "main.tsx",
    "server.ts",
    "server.js",
}
SOURCE_ROOT_HINTS = {
    "assets",
    "editor",
    "lib",
    "package",
    "packages",
    "runtime",
    "scripts",
    "src",
    "test",
    "tests",
}
SYMBOL_PATTERNS: dict[str, list[tuple[re.Pattern[str], str, int]]] = {
    ".py": [
        (re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "class", 1),
        (re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "function", 1),
    ],
    ".cs": [
        (
            re.compile(
                r"^\s*(?:\[[^\]]+\]\s*)*(?:(?:public|private|protected|internal|static|abstract|sealed|partial|new)\s+)*(class|interface|enum|struct|record)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
            ),
            "type",
            2,
        ),
        (
            re.compile(
                r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|sealed|partial|new)\s+)+[A-Za-z_<>\[\],?.]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
            ),
            "method",
            1,
        ),
    ],
    ".ts": [
        (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "class", 1),
        (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "interface", 1),
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\("), "function", 1),
    ],
    ".tsx": [
        (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "class", 1),
        (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "interface", 1),
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\("), "function", 1),
    ],
    ".js": [
        (re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "class", 1),
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\("), "function", 1),
    ],
    ".jsx": [
        (re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "class", 1),
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\("), "function", 1),
    ],
    ".java": [
        (re.compile(r"^\s*(?:(?:public|private|protected|abstract|final|static)\s+)*(class|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "type", 2),
    ],
    ".go": [
        (re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b"), "type", 1),
        (re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("), "function", 1),
    ],
    ".rs": [
        (re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "type", 1),
        (re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("), "function", 1),
    ],
}
MAX_SYMBOL_QUERY_TERMS = 10


def safe_path(workspace_root: Path, relative_path: str) -> Path:
    """解析并验证路径安全性.

    Args:
        workspace_root: 工作空间根目录。
        relative_path: 相对路径。

    Returns:
        解析后的绝对路径。

    Raises:
        ValueError: 如果路径尝试逃逸工作空间。
    """
    workspace_root = workspace_root.resolve()
    path = (workspace_root / relative_path).resolve()
    if not path.is_relative_to(workspace_root):
        raise ValueError(f"Path escapes workspace: {relative_path}")
    return path


def _read_text_with_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in READ_TEXT_ENCODINGS:
        try:
            text = raw.decode(encoding)
            return text.replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _should_skip_name(name: str, *, include_hidden: bool) -> bool:
    if not include_hidden and name.startswith("."):
        return True
    return name in EXPLORATION_IGNORED_DIR_NAMES


def _filtered_walk(base_path: Path, *, include_hidden: bool = False):
    for current_root, dir_names, file_names in os.walk(base_path):
        dir_names[:] = sorted(
            [
                name
                for name in dir_names
                if not _should_skip_name(name, include_hidden=include_hidden)
            ]
        )
        filtered_files = sorted(
            [
                name
                for name in file_names
                if include_hidden or not name.startswith(".")
            ]
        )
        yield Path(current_root), dir_names, filtered_files


def _relative_label(workspace_root: Path, path: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix() or "."
    except ValueError:
        return str(path)


def _nearest_existing_parent(path: Path, workspace_root: Path) -> Path:
    candidate = path.parent
    while candidate != candidate.parent:
        if candidate.exists():
            return candidate
        if candidate == workspace_root:
            break
        candidate = candidate.parent
    return workspace_root


def _path_candidates_for_missing_file(workspace_root: Path, requested_path: str, missing_path: Path, limit: int = 5) -> list[Path]:
    file_name = missing_path.name
    if not file_name:
        return []
    search_root = _nearest_existing_parent(missing_path, workspace_root)
    local_matches = [candidate for candidate in search_root.rglob(file_name) if candidate.is_file()]
    if local_matches:
        return local_matches[:limit]
    workspace_matches = [candidate for candidate in workspace_root.rglob(file_name) if candidate.is_file()]
    return workspace_matches[:limit]


def _fuzzy_path_candidates_for_missing_file(workspace_root: Path, missing_path: Path, limit: int = 5) -> list[Path]:
    file_name = missing_path.name
    stem = missing_path.stem
    if not file_name:
        return []
    search_root = _nearest_existing_parent(missing_path, workspace_root)
    local_files = [candidate for candidate in search_root.rglob("*") if candidate.is_file()]
    workspace_files = [candidate for candidate in workspace_root.rglob("*") if candidate.is_file()]
    pool = local_files or workspace_files
    if not pool:
        return []
    names = [candidate.name for candidate in pool]
    matched_names = difflib.get_close_matches(file_name, names, n=limit, cutoff=0.45)
    if not matched_names and stem:
        stems = [candidate.stem for candidate in pool]
        matched_stems = difflib.get_close_matches(stem, stems, n=limit, cutoff=0.45)
        matched_names = []
        for matched_stem in matched_stems:
            for candidate in pool:
                if candidate.stem == matched_stem:
                    matched_names.append(candidate.name)
    results: list[Path] = []
    seen: set[Path] = set()
    for matched_name in matched_names:
        for candidate in pool:
            if candidate.name != matched_name:
                continue
            if candidate in seen:
                continue
            results.append(candidate)
            seen.add(candidate)
            if len(results) >= limit:
                return results
    return results


def _format_missing_file_message(
    workspace_root: Path,
    requested_path: str,
    missing_path: Path,
    candidates: list[Path],
    *,
    fuzzy: bool = False,
) -> str:
    normalized_request = requested_path.replace("\\", "/")
    if not candidates:
        return f"Error: File not found: {normalized_request}"
    relative_candidates = [candidate.relative_to(workspace_root).as_posix() for candidate in candidates]
    if len(relative_candidates) == 1:
        return (
            f"[auto-resolved path] requested {normalized_request}, "
            f"using {relative_candidates[0]}"
        )
    lines = [f"Error: File not found: {normalized_request}", "Closest matches:" if not fuzzy else "Similar filenames:"]
    lines.extend(f"- {candidate}" for candidate in relative_candidates)
    return "\n".join(lines)


def read_file(ctx: Any, payload: dict[str, Any]) -> str:
    """读取文件内容.

    Args:
        ctx: 运行时上下文对象。
        payload: 包含 "path" 和可选 "limit" 的参数字典。

    Returns:
        文件内容字符串。
    """
    workspace_root = ctx.runtime.settings.workspace_root
    requested_path = str(payload["path"])
    path = safe_path(workspace_root, requested_path)
    limit = payload.get("limit")
    prefix = ""
    if not path.exists() or not path.is_file():
        candidates = _path_candidates_for_missing_file(workspace_root, requested_path, path)
        if len(candidates) != 1:
            if not candidates:
                fuzzy_candidates = _fuzzy_path_candidates_for_missing_file(workspace_root, path)
                if fuzzy_candidates:
                    return _format_missing_file_message(
                        workspace_root,
                        requested_path,
                        path,
                        fuzzy_candidates,
                        fuzzy=True,
                    )
            return _format_missing_file_message(workspace_root, requested_path, path, candidates)
        path = candidates[0]
        prefix = _format_missing_file_message(workspace_root, requested_path, path, candidates) + "\n\n"
    text = _read_text_with_fallback(path)
    lines = text.splitlines()
    if limit and limit < len(lines):
        lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
    content = "\n".join(lines)
    return f"{prefix}{content}"[: ctx.runtime.settings.runtime.max_tool_output_chars]


def tree_view(ctx: Any, payload: dict[str, Any]) -> str:
    workspace_root = ctx.runtime.settings.workspace_root
    base_path = safe_path(workspace_root, str(payload.get("path", ".")))
    if not base_path.exists():
        return f"Error: Path not found: {payload.get('path', '.')}"
    if not base_path.is_dir():
        return f"Error: Path is not a directory: {payload.get('path', '.')}"

    depth = max(0, int(payload.get("depth", 2)))
    limit = max(1, int(payload.get("limit", 200)))
    include_hidden = bool(payload.get("include_hidden", False))
    dirs_first = bool(payload.get("dirs_first", True))
    lines = [_relative_label(workspace_root, base_path) + "/"]
    shown = 0
    truncated = False

    def walk(current: Path, prefix: str, current_depth: int) -> None:
        nonlocal shown, truncated
        if current_depth >= depth or truncated:
            return
        try:
            entries = [entry for entry in current.iterdir() if not _should_skip_name(entry.name, include_hidden=include_hidden)]
        except OSError as exc:
            lines.append(f"{prefix}└── [error: {exc}]")
            return
        if dirs_first:
            entries.sort(key=lambda item: (0 if item.is_dir() else 1, item.name.lower()))
        else:
            entries.sort(key=lambda item: item.name.lower())
        for index, entry in enumerate(entries):
            connector = "└── " if index == len(entries) - 1 else "├── "
            child_prefix = prefix + ("    " if index == len(entries) - 1 else "│   ")
            label = entry.name + ("/" if entry.is_dir() else "")
            lines.append(prefix + connector + label)
            shown += 1
            if shown >= limit:
                truncated = True
                return
            if entry.is_dir():
                walk(entry, child_prefix, current_depth + 1)
                if truncated:
                    return

    walk(base_path, "", 0)
    if truncated:
        lines.append(f"... ({limit} entries shown)")
    if len(lines) == 1:
        lines.append("(empty directory)")
    return "\n".join(lines)[: ctx.runtime.settings.runtime.max_tool_output_chars]


def find_symbol(ctx: Any, payload: dict[str, Any]) -> str:
    workspace_root = ctx.runtime.settings.workspace_root
    base_path = safe_path(workspace_root, str(payload.get("path", ".")))
    if not base_path.exists():
        return f"Error: Path not found: {payload.get('path', '.')}"
    if not base_path.is_dir():
        return f"Error: Path is not a directory: {payload.get('path', '.')}"

    query = str(payload.get("query", "")).strip()
    if not query:
        return "Error: query is required."
    case_sensitive = bool(payload.get("case_sensitive", False))
    query_terms = [item.strip() for item in query.split("|")]
    query_terms = [item for item in query_terms if item]
    if not query_terms:
        return "Error: query is required."
    if len(query_terms) > MAX_SYMBOL_QUERY_TERMS:
        return f"Error: query supports at most {MAX_SYMBOL_QUERY_TERMS} terms separated by '|'."
    normalized_terms = query_terms if case_sensitive else [item.lower() for item in query_terms]
    limit = max(1, int(payload.get("limit", 50)))
    include_hidden = bool(payload.get("include_hidden", False))
    kind_filter = str(payload.get("kind", "")).strip().lower()
    results: list[str] = []
    truncated = False

    for current_root, _, file_names in _filtered_walk(base_path, include_hidden=include_hidden):
        for file_name in file_names:
            candidate = current_root / file_name
            extension = candidate.suffix.lower()
            patterns = SYMBOL_PATTERNS.get(extension, [])
            if not patterns:
                continue
            try:
                lines = _read_text_with_fallback(candidate).splitlines()
            except Exception:
                continue
            relative = candidate.relative_to(workspace_root).as_posix()
            for line_number, line in enumerate(lines, start=1):
                for pattern, default_kind, name_group in patterns:
                    match = pattern.search(line)
                    if not match:
                        continue
                    symbol_name = match.group(name_group)
                    detected_kind = match.group(1).lower() if default_kind == "type" and match.lastindex and match.lastindex >= 2 else default_kind
                    if kind_filter and detected_kind != kind_filter:
                        continue
                    haystack = symbol_name if case_sensitive else symbol_name.lower()
                    if not any(term in haystack for term in normalized_terms):
                        continue
                    results.append(f"{relative}:{line_number}:{detected_kind} {symbol_name}")
                    break
                if len(results) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    if not results:
        return "(no matches)"
    if truncated:
        results.append(f"... ({limit} matches shown)")
    return "\n".join(results)[: ctx.runtime.settings.runtime.max_tool_output_chars]


def project_scan(ctx: Any, payload: dict[str, Any]) -> str:
    workspace_root = ctx.runtime.settings.workspace_root
    base_path = safe_path(workspace_root, str(payload.get("path", ".")))
    if not base_path.exists():
        return f"Error: Path not found: {payload.get('path', '.')}"
    if not base_path.is_dir():
        return f"Error: Path is not a directory: {payload.get('path', '.')}"

    include_hidden = bool(payload.get("include_hidden", False))
    depth = max(1, int(payload.get("depth", 2)))
    max_results = max(1, int(payload.get("limit", 8)))
    ext_counts: Counter[str] = Counter()
    guidance_files: list[str] = []
    manifest_files: list[str] = []
    entry_candidates: list[str] = []
    file_count = 0
    dir_count = 0

    for current_root, dir_names, file_names in _filtered_walk(base_path, include_hidden=include_hidden):
        current_path = Path(current_root)
        rel_parts = current_path.relative_to(base_path).parts if current_path != base_path else ()
        dir_count += len(dir_names)
        for file_name in file_names:
            file_count += 1
            relative = (current_path / file_name).relative_to(workspace_root).as_posix()
            suffix = Path(file_name).suffix.lower()
            if suffix:
                ext_counts[suffix] += 1
            if file_name in GUIDANCE_FILENAMES and len(guidance_files) < max_results:
                guidance_files.append(relative)
            manifest_key = relative if relative in MANIFEST_FILENAMES else file_name
            if manifest_key in MANIFEST_FILENAMES and len(manifest_files) < max_results:
                manifest_files.append(relative)
            if file_name in ENTRY_FILE_NAMES and len(entry_candidates) < max_results:
                entry_candidates.append(relative)

    top_level_dirs: list[str] = []
    top_level_files: list[str] = []
    for entry in sorted(base_path.iterdir(), key=lambda item: (0 if item.is_dir() else 1, item.name.lower())):
        if _should_skip_name(entry.name, include_hidden=include_hidden):
            continue
        relative = entry.relative_to(workspace_root).as_posix()
        if entry.is_dir():
            top_level_dirs.append(relative + "/")
        else:
            top_level_files.append(relative)

    source_roots = [
        item
        for item in top_level_dirs
        if item.rstrip("/").split("/")[-1].lower() in SOURCE_ROOT_HINTS
    ][:max_results]

    stack_hints: list[str] = []
    if any(item.endswith(".cs") for item in ext_counts):
        stack_hints.append("C#")
    if any(item.endswith(".py") for item in ext_counts):
        stack_hints.append("Python")
    if any(item.endswith(".ts") or item.endswith(".tsx") or item.endswith(".js") for item in ext_counts):
        stack_hints.append("JavaScript/TypeScript")
    if any(path.endswith("Assets/") for path in top_level_dirs) and any(path.endswith("Packages/") for path in top_level_dirs):
        stack_hints.append("Unity")

    lines = [
        f"Project root: {_relative_label(workspace_root, base_path)}",
        f"Counts: {file_count} files, {dir_count} dirs",
    ]
    if stack_hints:
        lines.append(f"Likely stack: {', '.join(stack_hints)}")
    if guidance_files:
        lines.append("Guidance files:")
        lines.extend(f"- {item}" for item in guidance_files)
    if manifest_files:
        lines.append("Manifests:")
        lines.extend(f"- {item}" for item in manifest_files)
    if source_roots:
        lines.append("Likely source roots:")
        lines.extend(f"- {item}" for item in source_roots)
    if entry_candidates:
        lines.append("Entry candidates:")
        lines.extend(f"- {item}" for item in entry_candidates)
    if ext_counts:
        lines.append("Languages/files:")
        for extension, count in ext_counts.most_common(max_results):
            lines.append(f"- {extension}: {count}")
    lines.append("Tree:")
    lines.append(
        tree_view(
            ctx,
            {
                "path": str(payload.get("path", ".")),
                "depth": depth,
                "limit": max(20, max_results * 12),
                "include_hidden": include_hidden,
            },
        )
    )
    return "\n".join(lines)[: ctx.runtime.settings.runtime.max_tool_output_chars]


def _format_glob_no_matches(
    workspace_root: Path,
    base_path: Path,
    *,
    pattern: str,
    recursive: bool,
    match_type: str,
    type_filtered_matches: int,
) -> str:
    try:
        base_label = base_path.relative_to(workspace_root).as_posix() or "."
    except ValueError:
        base_label = str(base_path)
    lines = [
        "(no matches)",
        f"path: {base_label}",
        f"pattern: {pattern}",
        f"match: {match_type}, recursive: {str(recursive).lower()}",
    ]
    if type_filtered_matches:
        opposite = "dirs" if match_type == "files" else "files"
        lines.append(f"Matched path segments exist, but they were filtered out by `match={match_type}`. Try `match={opposite}` or `match=all`.")
    elif "/" in pattern and not recursive:
        lines.append("Pattern includes subdirectories. With `recursive=false`, only explicit path segments in the pattern are searched; no full recursive walk is performed.")
    elif not recursive:
        lines.append("Only the direct children under `path` were searched. Set `recursive=true` to walk deeper.")
    else:
        lines.append("Try a broader pattern or narrow `path` closer to the expected location.")
    return "\n".join(lines)


_GREP_REGEX_CHAR_CLASS_PATTERN = re.compile(r"(?<!\\)\[[^\]]+\]")
_GREP_REGEX_GROUP_PATTERN = re.compile(r"(?<!\\)\([^)]*\)")
_GREP_REGEX_ESCAPED_CLASS_WITH_QUANTIFIER_PATTERN = re.compile(r"\\[dDsSwW](?:[+*?]|\{[0-9]+(?:,[0-9]*)?\})")
_GREP_REGEX_WORD_BOUNDARY_PATTERN = re.compile(r"\\b[^\\]+\\b")
_GREP_REGEX_ANCHOR_ESCAPE_PATTERN = re.compile(r"^(?:\\A.*|.*\\Z)$")
_GREP_REGEX_QUANTIFIER_PATTERN = re.compile(r"(?<!\\)(?:\.\*|\.\+|\.\?|(?<![A-Za-z0-9_])\{[0-9]+(?:,[0-9]*)?\})")

GREP_TOOL_DESCRIPTION = (
    "Search file contents inside the workspace and return matching lines. "
    "Obvious regex patterns such as `foo|bar`, `^name$`, `\\berror\\b`, or `\\d+` "
    "are auto-detected; set `use_regex=false` to force literal substring matching."
)


def _grep_pattern_looks_regex_like(pattern: str) -> bool:
    if "|" in pattern:
        return True
    if pattern.startswith("^") or pattern.endswith("$"):
        return True
    if _GREP_REGEX_CHAR_CLASS_PATTERN.search(pattern):
        return True
    if _GREP_REGEX_GROUP_PATTERN.search(pattern):
        return True
    if _GREP_REGEX_ESCAPED_CLASS_WITH_QUANTIFIER_PATTERN.search(pattern):
        return True
    if _GREP_REGEX_WORD_BOUNDARY_PATTERN.search(pattern):
        return True
    if _GREP_REGEX_ANCHOR_ESCAPE_PATTERN.search(pattern):
        return True
    if _GREP_REGEX_QUANTIFIER_PATTERN.search(pattern):
        return True
    return False


def _compile_grep_matcher(
    pattern: str,
    *,
    flags: int,
    use_regex: bool,
    explicit_use_regex: bool,
) -> tuple[re.Pattern[str] | None, str | None]:
    if not use_regex:
        return None, None
    try:
        return re.compile(pattern, flags), None
    except re.error as exc:
        if explicit_use_regex:
            return None, f"Error: invalid regex pattern: {exc}"
        return None, None


def glob_search(ctx: Any, payload: dict[str, Any]) -> str:
    workspace_root = ctx.runtime.settings.workspace_root
    base_path = safe_path(workspace_root, str(payload.get("path", ".")))
    if not base_path.exists():
        return f"Error: Path not found: {payload.get('path', '.')}"
    if not base_path.is_dir():
        return f"Error: Path is not a directory: {payload.get('path', '.')}"

    pattern = str(payload["pattern"]).strip()
    recursive = bool(payload.get("recursive", True))
    match_type = str(payload.get("match", "files")).strip().lower()
    limit = max(1, int(payload.get("limit", 100)))
    if match_type not in {"files", "dirs", "all"}:
        return "Error: match must be one of 'files', 'dirs', or 'all'."

    normalized_pattern = pattern.replace("\\", "/")
    iterators = [base_path.glob(normalized_pattern)]
    if recursive and "/" not in normalized_pattern and "**" not in normalized_pattern:
        iterators.append(base_path.rglob(normalized_pattern))

    results: list[str] = []
    seen: set[Path] = set()
    type_filtered_matches = 0
    truncated = False
    for iterator in iterators:
        for candidate in iterator:
            if candidate in seen:
                continue
            seen.add(candidate)
            is_dir = candidate.is_dir()
            if match_type == "files" and is_dir:
                type_filtered_matches += 1
                continue
            if match_type == "dirs" and not is_dir:
                type_filtered_matches += 1
                continue
            relative = candidate.relative_to(workspace_root).as_posix()
            results.append(relative + ("/" if is_dir else ""))
            if len(results) >= limit:
                truncated = True
                break
        if truncated:
            break
    if not results:
        return _format_glob_no_matches(
            workspace_root,
            base_path,
            pattern=normalized_pattern,
            recursive=recursive,
            match_type=match_type,
            type_filtered_matches=type_filtered_matches,
        )
    if truncated:
        results.append(f"... ({limit} results shown)")
    return "\n".join(results)[: ctx.runtime.settings.runtime.max_tool_output_chars]


def grep_search(ctx: Any, payload: dict[str, Any]) -> str:
    workspace_root = ctx.runtime.settings.workspace_root
    base_path = safe_path(workspace_root, str(payload.get("path", ".")))
    if not base_path.exists():
        return f"Error: Path not found: {payload.get('path', '.')}"
    if not base_path.is_dir():
        return f"Error: Path is not a directory: {payload.get('path', '.')}"

    pattern = str(payload["pattern"])
    glob_pattern = str(payload.get("glob", "*"))
    recursive = bool(payload.get("recursive", True))
    case_sensitive = bool(payload.get("case_sensitive", False))
    explicit_use_regex = "use_regex" in payload
    use_regex = bool(payload.get("use_regex", False))
    if not explicit_use_regex and _grep_pattern_looks_regex_like(pattern):
        use_regex = True
    limit = max(1, int(payload.get("limit", 50)))

    flags = 0 if case_sensitive else re.IGNORECASE
    matcher, compile_error = _compile_grep_matcher(
        pattern,
        flags=flags,
        use_regex=use_regex,
        explicit_use_regex=explicit_use_regex,
    )
    if compile_error is not None:
        return compile_error
    needle = pattern if case_sensitive else pattern.lower()

    iterator = base_path.rglob("*") if recursive else base_path.glob("*")
    matches: list[str] = []
    truncated = False
    for candidate in iterator:
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(workspace_root).as_posix()
        if not (fnmatch.fnmatch(relative, glob_pattern) or fnmatch.fnmatch(candidate.name, glob_pattern)):
            continue
        try:
            lines = _read_text_with_fallback(candidate).splitlines()
        except Exception:
            continue
        for line_number, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.lower()
            found = bool(matcher.search(line)) if matcher is not None else needle in haystack
            if not found:
                continue
            matches.append(f"{relative}:{line_number}:{line}")
            if len(matches) >= limit:
                truncated = True
                break
        if truncated:
            break
    if not matches:
        return "(no matches)"
    if truncated:
        matches.append(f"... ({limit} matches shown)")
    return "\n".join(matches)[: ctx.runtime.settings.runtime.max_tool_output_chars]


def _line_diff_stats(before: str, after: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


def _record_file_change(ctx: Any, record: dict[str, Any]) -> None:
    session = getattr(ctx, "session", None)
    if session is None:
        return
    pending = getattr(session, "pending_file_changes", None)
    if pending is None:
        return
    pending.append(record)


def write_file(ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
    path = safe_path(ctx.runtime.settings.workspace_root, payload["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    content = str(payload["content"])
    existed_before = path.exists()
    previous = _read_text_with_fallback(path) if existed_before else ""
    path.write_text(content, encoding="utf-8")
    added, removed = _line_diff_stats(previous, content)
    _record_file_change(
        ctx,
        {
            "tool_name": "write_file",
            "path": payload["path"],
            "absolute_path": str(path),
            "added_lines": added,
            "removed_lines": removed,
            "existed_before": existed_before,
            "previous_content": previous,
        },
    )
    return {
        "status": "ok",
        "action": "write_file",
        "path": payload["path"],
        "absolute_path": str(path),
        "existed_before": existed_before,
        "added_lines": added,
        "removed_lines": removed,
        "bytes_written": len(content),
    }


def edit_file(ctx: Any, payload: dict[str, Any]) -> dict[str, Any] | str:
    path = safe_path(ctx.runtime.settings.workspace_root, payload["path"])
    old_text = str(payload["old_text"])
    new_text = str(payload["new_text"])
    content = _read_text_with_fallback(path)
    if old_text not in content:
        return f"Error: Text not found in {payload['path']}"
    updated = content.replace(old_text, new_text, 1)
    path.write_text(updated, encoding="utf-8")
    added, removed = _line_diff_stats(content, updated)
    _record_file_change(
        ctx,
        {
            "tool_name": "edit_file",
            "path": payload["path"],
            "absolute_path": str(path),
            "added_lines": added,
            "removed_lines": removed,
            "existed_before": True,
            "previous_content": content,
        },
    )
    return {
        "status": "ok",
        "action": "edit_file",
        "path": payload["path"],
        "absolute_path": str(path),
        "added_lines": added,
        "removed_lines": removed,
    }


def register_filesystem_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="project_scan",
            description="Build a concise project map: likely stacks, guidance files, manifests, source roots, entry candidates, language/file counts, and a shallow tree. Prefer this at the start of repository exploration.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "depth": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "include_hidden": {"type": "boolean"},
                },
            },
            handler=project_scan,
        )
    )
    registry.register(
        ToolDefinition(
            name="tree",
            description="Render a shallow directory tree for a focused path. Use this to build a mental map before reading files or falling back to broad glob patterns.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "depth": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "dirs_first": {"type": "boolean"},
                    "include_hidden": {"type": "boolean"},
                },
            },
            handler=tree_view,
        )
    )
    registry.register(
        ToolDefinition(
            name="find_symbol",
            description="Locate classes, interfaces, structs, records, functions, or methods by symbol name substring across common code file types. `query` also supports up to 10 alternative substrings joined by `|` for one broad pass before narrowing down.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "kind": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "include_hidden": {"type": "boolean"},
                },
                "required": ["query"],
            },
            handler=find_symbol,
        )
    )
    registry.register(
        ToolDefinition(
            name="glob",
            description="Search for files or directories by glob pattern inside the workspace. Prefer focused patterns like exact filenames, suffix filters such as `**/*.cs`, or narrowed directories. Avoid broad `**/*` enumeration unless you truly need a full tree dump.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "match": {"type": "string", "enum": ["files", "dirs", "all"]},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
            handler=glob_search,
        )
    )
    registry.register(
        ToolDefinition(
            name="grep",
            description=GREP_TOOL_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "use_regex": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
            handler=grep_search,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_file",
            description="Read file contents. Before using this, confirm the exact path with a focused `glob` instead of guessing from broad listings. If the path is missing and there is exactly one filename match nearby, this tool will auto-resolve it.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
            handler=read_file,
        )
    )
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
            handler=write_file,
        )
    )
    registry.register(
        ToolDefinition(
            name="edit_file",
            description="Replace exact text in a file once. Confirm the exact path with a focused `glob` before editing; do not guess paths from broad directory listings.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            handler=edit_file,
        )
    )
