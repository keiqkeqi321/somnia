from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectInstructions:
    source_path: Path
    content: str
    truncated: bool = False
    scope_path: Path | None = None


class ProjectInstructionsLoader:
    FILENAMES = ("AGENTS.md", "CLAUDE.md")

    def __init__(self, workspace_root: Path, max_chars: int = 65_536) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.max_chars = max_chars

    def load(self) -> ProjectInstructions | None:
        return self._load_from_dir(self.workspace_root)

    def load_scoped(self, paths: list[str | Path] | tuple[str | Path, ...] | None = None) -> list[ProjectInstructions]:
        directories = self._instruction_directories(paths)
        instructions: list[ProjectInstructions] = []
        seen: set[Path] = set()
        for directory in directories:
            item = self._load_from_dir(directory)
            if item is None:
                continue
            source = item.source_path.resolve()
            if source in seen:
                continue
            seen.add(source)
            instructions.append(item)
        return instructions

    def _load_from_dir(self, directory: Path) -> ProjectInstructions | None:
        for filename in self.FILENAMES:
            path = directory / filename
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            truncated = len(text) > self.max_chars
            if truncated:
                text = text[: self.max_chars].rstrip()
            return ProjectInstructions(source_path=path, content=text, truncated=truncated, scope_path=directory)
        return None

    def _instruction_directories(self, paths: list[str | Path] | tuple[str | Path, ...] | None) -> list[Path]:
        root = self.workspace_root.resolve()
        directories = [root]
        for raw_path in paths or ():
            if raw_path is None:
                continue
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                resolved = candidate.resolve()
                relative = resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            current = root
            parts = list(relative.parts)
            if parts and "." in parts[-1]:
                parts = parts[:-1]
            for part in parts:
                current = current / part
                if current not in directories:
                    directories.append(current)
        return directories

    def render(self, paths: list[str | Path] | tuple[str | Path, ...] | None = None) -> str:
        instructions = self.load_scoped(paths)
        if not instructions:
            return ""
        rendered_blocks: list[str] = []
        for item in instructions:
            source_name = item.source_path.name if item.source_path.parent == self.workspace_root else item.source_path.relative_to(self.workspace_root).as_posix()
            scope = "."
            if item.scope_path is not None:
                try:
                    scope = item.scope_path.relative_to(self.workspace_root).as_posix() or "."
                except ValueError:
                    scope = str(item.scope_path)
            content = item.content
            if item.truncated:
                content = f"{content}\n\n[Project instructions truncated at {self.max_chars} characters.]"
            rendered_blocks.append(
                f"<project-instructions source=\"{source_name}\" scope=\"{scope}\">\n"
                f"{content}\n"
                "</project-instructions>"
            )
        return (
            "Project instructions:\n"
            "Follow these workspace-specific instructions unless they conflict with higher-priority "
            "runtime, execution-mode, or tool safety rules. When multiple project instruction files apply, "
            "more specific directory scopes override broader scopes.\n"
            + "\n".join(rendered_blocks)
        )
