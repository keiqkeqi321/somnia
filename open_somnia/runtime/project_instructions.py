from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectInstructions:
    source_path: Path
    content: str
    truncated: bool = False


class ProjectInstructionsLoader:
    FILENAMES = ("AGENTS.md", "CLAUDE.md")

    def __init__(self, workspace_root: Path, max_chars: int = 65_536) -> None:
        self.workspace_root = Path(workspace_root)
        self.max_chars = max_chars

    def load(self) -> ProjectInstructions | None:
        for filename in self.FILENAMES:
            path = self.workspace_root / filename
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
            return ProjectInstructions(source_path=path, content=text, truncated=truncated)
        return None

    def render(self) -> str:
        instructions = self.load()
        if instructions is None:
            return ""
        source_name = instructions.source_path.name
        content = instructions.content
        if instructions.truncated:
            content = f"{content}\n\n[Project instructions truncated at {self.max_chars} characters.]"
        return (
            "Project instructions:\n"
            "Follow these workspace-specific instructions unless they conflict with higher-priority "
            "runtime, execution-mode, or tool safety rules.\n"
            f"<project-instructions source=\"{source_name}\">\n"
            f"{content}\n"
            "</project-instructions>"
        )
