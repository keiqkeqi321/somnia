from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

DEFAULT_SKILL_PROMPT_DESCRIPTION_CHARS = 96
DEFAULT_SKILL_PROMPT_ENTRY_LIMIT = 24


class SkillLoader:
    def __init__(self, skill_dirs: Path | Iterable[Path]):
        if isinstance(skill_dirs, Path):
            self.skill_dirs = [skill_dirs]
        else:
            self.skill_dirs = [Path(path) for path in skill_dirs]
        self.skills: dict[str, dict] = {}
        self.reload()

    @classmethod
    def for_workspace(cls, workspace_root: Path) -> "SkillLoader":
        return cls(
            [
                Path(__file__).resolve().parent / "builtin",
                Path.home() / ".claude" / "skills",
                Path.home() / ".open_somnia" / "skills",
                workspace_root / "skills",
                workspace_root / ".claude" / "skills",
                workspace_root / ".open_somnia" / "skills",
            ]
        )

    def reload(self) -> None:
        self.skills = {}
        for source_dir in self.skill_dirs:
            if not source_dir.exists():
                continue
            for path in self._iter_skill_files(source_dir):
                text = path.read_text(encoding="utf-8")
                meta, body = self._parse(text)
                name = path.parent.name
                self.skills[name.casefold()] = {
                    "name": name,
                    "meta": meta,
                    "body": body,
                    "path": path,
                    "scope": self._scope_name(source_dir),
                }

    def _iter_skill_files(self, source_dir: Path) -> list[Path]:
        return sorted(
            [path for path in source_dir.rglob("*") if path.is_file() and path.name.casefold() == "skill.md"],
            key=lambda path: (len(path.parts), str(path).lower()),
        )

    def _scope_name(self, source_dir: Path) -> str:
        builtin_skills_dir = Path(__file__).resolve().parent / "builtin"
        if source_dir == builtin_skills_dir:
            return "builtin"
        home_skills_dir = Path.home() / ".open_somnia" / "skills"
        if source_dir == home_skills_dir:
            return "global"
        home_claude_skills_dir = Path.home() / ".claude" / "skills"
        if source_dir == home_claude_skills_dir:
            return "global-claude"
        if source_dir.name == "skills" and source_dir.parent.name == ".open_somnia":
            return "workspace"
        if source_dir.name == "skills" and source_dir.parent.name == ".claude":
            return "workspace-claude"
        if source_dir.name == "skills":
            return "workspace-legacy"
        return "custom"

    def _parse(self, text: str) -> tuple[dict[str, str], str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text.strip()
        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2).strip()

    def descriptions(self) -> str:
        self.reload()
        if not self.skills:
            return "(no skills)"
        return "\n".join(
            f"- {skill['name']}: {skill['meta'].get('description', '-')}"
            for skill in sorted(self.skills.values(), key=lambda item: item["name"].casefold())
        )

    def prompt_index(
        self,
        *,
        max_description_chars: int = DEFAULT_SKILL_PROMPT_DESCRIPTION_CHARS,
        max_entries: int = DEFAULT_SKILL_PROMPT_ENTRY_LIMIT,
    ) -> str:
        self.reload()
        if not self.skills:
            return "(no skills)\nUse `load_skill` only when specialized guidance is needed."
        entries = sorted(self.skills.values(), key=lambda item: item["name"].casefold())
        limit = max(1, int(max_entries))
        description_limit = max(24, int(max_description_chars))
        lines = [
            "Skill index (summaries only; full instructions are lazy-loaded):",
            "Use `load_skill` with a skill name when a task matches one of these summaries.",
        ]
        for skill in entries[:limit]:
            description = self._compact_description(skill["meta"].get("description", "-"), description_limit)
            lines.append(f"- {skill['name']}: {description}")
        omitted = len(entries) - limit
        if omitted > 0:
            lines.append(f"- ... {omitted} more skill(s) omitted from the prompt index. Use `/skills` or `load_skill` when needed.")
        return "\n".join(lines)

    def _compact_description(self, value: object, max_chars: int) -> str:
        description = re.sub(r"\s+", " ", str(value or "-")).strip() or "-"
        if len(description) <= max_chars:
            return description
        return description[: max_chars - 3].rstrip() + "..."

    def load(self, name: str) -> str:
        self.reload()
        skill = self.skills.get(name.casefold())
        if not skill:
            available = ", ".join(self.names())
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f"<skill name=\"{skill['name']}\">\n{skill['body']}\n</skill>"

    def names(self) -> list[str]:
        self.reload()
        return [skill["name"] for skill in sorted(self.skills.values(), key=lambda item: item["name"].casefold())]

    def list_entries(self) -> list[dict[str, str]]:
        return [
            {
                "name": skill["name"],
                "description": skill["meta"].get("description", "-"),
                "path": str(skill["path"]),
                "scope": str(skill["scope"]),
            }
            for skill in sorted(self.skills.values(), key=lambda item: item["name"].casefold())
        ]

    def render_listing(self) -> str:
        self.reload()
        entries = self.list_entries()
        if not entries:
            return "No skills."
        lines: list[str] = []
        for entry in entries:
            lines.append(f"- {entry['name']} [{entry['scope']}] - {entry['description']}")
            lines.append(f"  use: /+{entry['name']}")
            lines.append(f"  path: {entry['path']}")
        return "\n".join(lines)
