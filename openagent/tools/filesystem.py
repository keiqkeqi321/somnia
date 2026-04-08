"""文件系统工具模块.

提供文件读写、编辑等操作的工具函数。
"""

from __future__ import annotations

import difflib
import fnmatch
from pathlib import Path
import re
from typing import Any

from openagent.tools.registry import ToolDefinition

READ_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "cp936")


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
    use_regex = bool(payload.get("use_regex", False))
    limit = max(1, int(payload.get("limit", 50)))

    flags = 0 if case_sensitive else re.IGNORECASE
    matcher = re.compile(pattern, flags) if use_regex else None
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
            description="Search file contents inside the workspace and return matching lines.",
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
