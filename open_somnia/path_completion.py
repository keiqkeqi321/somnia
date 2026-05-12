from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re


IGNORED_PATH_COMPLETION_DIR_NAMES = {
    ".git",
    ".local-tools",
    ".open_somnia",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".tox",
    "__pycache__",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "target",
    "tmp",
}

MAX_PATH_COMPLETION_CANDIDATES = 20_000
PATH_COMPLETION_CACHE_SECONDS = 5.0
TEMPORARY_ROOT_DIR_PATTERN = re.compile(r"^tmp[a-z0-9_]{8}$", re.IGNORECASE)


@dataclass(slots=True)
class PathCandidate:
    relative_path: str
    basename: str
    kind: str


def is_ignored_path_completion_name(name: str, *, is_root_child: bool = False) -> bool:
    if name in IGNORED_PATH_COMPLETION_DIR_NAMES or name.startswith(".tmp-"):
        return True
    return is_root_child and bool(TEMPORARY_ROOT_DIR_PATTERN.match(name))


def scan_path_completion_candidates(
    workspace_root: Path,
    *,
    max_depth: int | None = None,
    max_candidates: int = MAX_PATH_COMPLETION_CANDIDATES,
) -> list[PathCandidate]:
    candidates: list[PathCandidate] = []
    stack: list[tuple[Path, tuple[str, ...]]] = [(workspace_root, ())]
    while stack and len(candidates) < max_candidates:
        current, relative_parts = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        entries.sort(key=lambda entry: entry.name.lower())
        for entry in entries:
            if len(candidates) >= max_candidates:
                break
            name = entry.name
            if is_ignored_path_completion_name(name, is_root_child=not relative_parts):
                continue
            child_parts = (*relative_parts, name)
            relative = "/".join(child_parts)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                candidates.append(PathCandidate(relative_path=relative, basename=name, kind="dir"))
                if max_depth is None or len(child_parts) < max_depth:
                    stack.append((Path(entry.path), child_parts))
            elif is_file:
                candidates.append(PathCandidate(relative_path=relative, basename=name, kind="file"))
    return candidates


def sort_path_completion_candidates(candidates: list[PathCandidate]) -> list[PathCandidate]:
    return sorted(
        candidates,
        key=lambda item: (0 if item.kind == "dir" else 1, len(item.relative_path), item.relative_path),
    )


def match_path_completion_candidates(
    candidates: list[PathCandidate],
    query: str,
    *,
    limit: int = 30,
) -> list[PathCandidate]:
    lowered = str(query or "").lower()
    matches = candidates
    if lowered:
        matches = [
            candidate
            for candidate in candidates
            if lowered in candidate.relative_path.lower() or lowered in candidate.basename.lower()
        ]
    return sorted(matches, key=lambda item: path_completion_score(item, lowered))[:limit]


def path_completion_score(item: PathCandidate, query: str) -> tuple[int, int, int, int, str]:
    basename = item.basename.lower()
    path = item.relative_path.lower()
    basename_starts = 0 if query and basename.startswith(query) else 1
    basename_contains = 0 if query and query in basename else 1
    kind_rank = 0 if item.kind == "dir" else 1
    return (basename_starts, basename_contains, kind_rank, len(path), path)
