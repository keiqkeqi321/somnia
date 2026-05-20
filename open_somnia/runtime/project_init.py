from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib


SKIP_DIR_NAMES = {
    ".git",
    ".gitnexus",
    ".hg",
    ".mypy_cache",
    ".open_somnia",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tmp",
    ".tmp-tests",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}
SOURCE_ROOT_NAMES = {"app", "apps", "desktop", "lib", "open_somnia", "packages", "scripts", "src", "tests"}
CODE_EXTENSIONS = {
    ".cs",
    ".cpp",
    ".c",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
}
MANIFEST_NAMES = {
    "Cargo.toml",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "tox.ini",
    "uv.lock",
}
GITNEXUS_BLOCK_START = "<!-- gitnexus:start -->"
GITNEXUS_BLOCK_END = "<!-- gitnexus:end -->"


@dataclass(frozen=True, slots=True)
class ProjectInitPrompt:
    target_path: Path
    prompt: str
    line_limit: int
    code_file_count: int
    force: bool = False
    extra_prompt: str = ""
    protected_gitnexus_block: str = ""


@dataclass(slots=True)
class ProjectProfile:
    file_count: int
    dir_count: int
    code_file_count: int
    extension_counts: Counter[str]
    top_level_dirs: list[str]
    manifests: list[str]
    source_roots: list[str]
    test_roots: list[str]
    entry_points: list[str]
    project_name: str
    description: str
    console_scripts: list[str]


def init_line_limit(code_file_count: int) -> int:
    if code_file_count <= 80:
        return 60
    if code_file_count <= 300:
        return 80
    if code_file_count <= 1_200:
        return 120
    if code_file_count <= 5_000:
        return 160
    return 200


def build_project_init_prompt(workspace_root: Path, *, force: bool = False, extra_prompt: str = "") -> ProjectInitPrompt:
    root = Path(workspace_root)
    profile = _scan_project(root)
    line_limit = init_line_limit(profile.code_file_count)
    target = root / "AGENTS.md"
    normalized_extra_prompt = str(extra_prompt or "").strip()
    protected_gitnexus_block = _extract_gitnexus_block(target)
    prompt = _render_init_prompt(
        profile,
        target=target,
        line_limit=line_limit,
        force=force,
        extra_prompt=normalized_extra_prompt,
        protected_gitnexus_block=protected_gitnexus_block,
    )
    return ProjectInitPrompt(
        target_path=target,
        prompt=prompt,
        line_limit=line_limit,
        code_file_count=profile.code_file_count,
        force=force,
        extra_prompt=normalized_extra_prompt,
        protected_gitnexus_block=protected_gitnexus_block,
    )


def _scan_project(root: Path) -> ProjectProfile:
    extension_counts: Counter[str] = Counter()
    top_level_dirs = _top_level_dirs(root)
    manifests: list[str] = []
    source_roots: list[str] = []
    test_roots: list[str] = []
    entry_points: list[str] = []
    file_count = 0
    dir_count = 0
    code_file_count = 0

    for current_root, dir_names, file_names in os.walk(root, onerror=lambda _error: None):
        current_path = Path(current_root)
        dir_names[:] = [
            name
            for name in dir_names
            if name not in SKIP_DIR_NAMES and not (name.startswith("tmp") and len(name) > 3)
        ]
        dir_count += len(dir_names)
        rel_current = "." if current_path == root else current_path.relative_to(root).as_posix()
        if rel_current != ".":
            leaf = current_path.name.lower()
            if leaf in SOURCE_ROOT_NAMES and len(source_roots) < 10:
                source_roots.append(rel_current)
            if leaf in {"test", "tests", "__tests__"} and len(test_roots) < 8:
                test_roots.append(rel_current)
        for file_name in file_names:
            path = current_path / file_name
            rel = path.relative_to(root).as_posix()
            file_count += 1
            suffix = path.suffix.lower()
            if suffix:
                extension_counts[suffix] += 1
            if suffix in CODE_EXTENSIONS:
                code_file_count += 1
            if file_name in MANIFEST_NAMES and len(manifests) < 12:
                manifests.append(rel)
            if file_name in {"main.py", "cli.py", "app.py", "server.py", "index.ts", "index.js"} and len(entry_points) < 10:
                entry_points.append(rel)

    pyproject = _read_pyproject(root / "pyproject.toml")
    project = pyproject.get("project", {}) if isinstance(pyproject.get("project"), dict) else {}
    scripts = project.get("scripts", {}) if isinstance(project.get("scripts"), dict) else {}
    return ProjectProfile(
        file_count=file_count,
        dir_count=dir_count,
        code_file_count=code_file_count,
        extension_counts=extension_counts,
        top_level_dirs=top_level_dirs,
        manifests=manifests,
        source_roots=source_roots,
        test_roots=test_roots,
        entry_points=entry_points,
        project_name=str(project.get("name") or root.name),
        description=str(project.get("description") or ""),
        console_scripts=[f"{name} = {target}" for name, target in sorted(scripts.items())],
    )


def _top_level_dirs(root: Path) -> list[str]:
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    dirs = [
        entry.name
        for entry in entries
        if entry.is_dir() and entry.name not in SKIP_DIR_NAMES and not (entry.name.startswith("tmp") and len(entry.name) > 3)
    ]
    return sorted(dirs, key=str.casefold)[:14]


def _read_pyproject(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _extract_gitnexus_block(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    start = text.find(GITNEXUS_BLOCK_START)
    if start < 0:
        return ""
    end = text.find(GITNEXUS_BLOCK_END, start + len(GITNEXUS_BLOCK_START))
    if end < 0:
        return ""
    return text[start : end + len(GITNEXUS_BLOCK_END)]


def _render_init_prompt(
    profile: ProjectProfile,
    *,
    target: Path,
    line_limit: int,
    force: bool,
    extra_prompt: str = "",
    protected_gitnexus_block: str = "",
) -> str:
    lines = [
        "Initialize project instructions for this workspace.",
        "",
        "You must run a real repository inspection loop before writing the file.",
        "Use tools such as project_scan, tree, read_file, glob, grep, and find_symbol to inspect actual code, manifests, tests, and entry points.",
        "Do not rely only on the snapshot below; treat it as a starting hint.",
        "",
        f"Target file: {target.name}",
        f"Overwrite existing file: {'yes' if force else 'no; if it exists, read it first and preserve useful project-specific guidance'}",
        f"Hard line budget: {line_limit} lines maximum.",
        "",
    ]
    if extra_prompt:
        lines.extend(
            [
                "User extra instructions for this initialization:",
                extra_prompt,
                "",
                "Treat the user extra instructions as preferences for what to inspect or emphasize. Do not copy them verbatim into AGENTS.md unless they are useful project guidance verified against the repository.",
                "If user extra instructions conflict with the hard line budget, real inspection requirement, or safety rules, those hard constraints win.",
                "",
            ]
        )
    if protected_gitnexus_block:
        lines.extend(
            [
                "Protected indexed guidance detected in existing AGENTS.md:",
                protected_gitnexus_block,
                "",
                "Code-level preservation requirement:",
                f"- The complete block from {GITNEXUS_BLOCK_START} through {GITNEXUS_BLOCK_END} is protected indexed guidance.",
                "- Preserve that block byte-for-byte in the rewritten AGENTS.md, even when overwrite is enabled.",
                "- You may move the whole block only if needed, but must not edit, summarize, duplicate, or delete any line inside it.",
                "",
            ]
        )
    lines.extend(
        [
            "Line budget rule used by Somnia:",
            "- <=80 code files: 60 lines",
            "- <=300 code files: 80 lines",
            "- <=1200 code files: 120 lines",
            "- <=5000 code files: 160 lines",
            "- >5000 code files: 200 lines",
            "",
            "Local snapshot:",
            f"- Project name: {profile.project_name}",
            f"- Description: {profile.description or '(not declared)'}",
            f"- Scale: {profile.code_file_count} code files, {profile.file_count} total files, {profile.dir_count} directories",
            f"- Languages/extensions: {_format_extension_counts(profile.extension_counts)}",
            f"- Manifests: {_format_list(profile.manifests)}",
            f"- Source roots: {_format_list(profile.source_roots)}",
            f"- Test roots: {_format_list(profile.test_roots)}",
            f"- Console scripts: {_format_list(profile.console_scripts)}",
            f"- Entry candidates: {_format_list(profile.entry_points)}",
            f"- Top-level dirs: {_format_list(profile.top_level_dirs)}",
            "",
            "Required output file properties:",
            "- Write AGENTS.md using write_file or edit_file; do not merely print proposed content.",
            "- Keep it concise and useful as system-prompt context.",
            "- Include only facts you verified from files or clearly label uncertainty.",
            "- Prefer concrete commands and paths over generic advice.",
            "- Do not include marketing copy, long architecture essays, duplicated sections, or generated noise.",
            "- Do not include secrets, tokens, environment variable values, or private machine-specific paths unless already part of repo docs.",
            "- Mention that higher-priority user/runtime/tool safety instructions override this file.",
            "- Include build/test commands only if you verified them from manifests, docs, or tests.",
            "- If AGENTS.md already has useful hand-written guidance, merge rather than deleting it blindly.",
            f"- Preserve any existing {GITNEXUS_BLOCK_START} ... {GITNEXUS_BLOCK_END} block exactly.",
            "- After writing AGENTS.md, verify the line count. If it exceeds the hard line budget, edit it down before finishing.",
            "",
            "Recommended structure:",
            "1. Project purpose and main execution path",
            "2. Important directories and entry points",
            "3. Build, test, and verification commands",
            "4. Editing rules and compatibility cautions",
            "5. Any repo-specific workflows, generated files, or storage notes",
            "",
            "After writing the file, final response must be short: path, line count, and the key evidence inspected. Do not paste the full AGENTS.md content.",
        ]
    )
    return "\n".join(lines)


def _format_extension_counts(extension_counts: Counter[str]) -> str:
    if not extension_counts:
        return "(none detected)"
    return ", ".join(f"{extension}:{count}" for extension, count in extension_counts.most_common(10))


def _format_list(items: list[str]) -> str:
    if not items:
        return "(none detected)"
    shown = items[:10]
    suffix = f", +{len(items) - len(shown)} more" if len(items) > len(shown) else ""
    return ", ".join(shown) + suffix
