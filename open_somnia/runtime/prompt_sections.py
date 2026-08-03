from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class PromptSection:
    id: str
    title: str
    content: str
    dynamic: bool = False

    def render(self) -> str:
        body = self.content.strip()
        if not body:
            return ""
        return f"## {self.title}\n{body}"

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "dynamic": self.dynamic,
            "chars": len(self.content),
            "lines": len(self.content.splitlines()),
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class PromptBundle:
    sections: tuple[PromptSection, ...]

    def render(self) -> str:
        return "\n\n".join(rendered for section in self.sections if (rendered := section.render()))

    def to_payload(self) -> list[dict[str, object]]:
        return [section.to_payload() for section in self.sections if section.content.strip()]


# Title prefixes of session-stable sections, in emission order. SystemPromptBuilder
# emits these sections first (stable-first layout for prompt caching); any other
# section is treated as dynamic when a rendered prompt is parsed back into sections.
STABLE_SECTION_TITLE_PREFIXES = ("A.", "B.", "C.")


def section_title_is_stable(title: str) -> bool:
    return str(title or "").strip().startswith(STABLE_SECTION_TITLE_PREFIXES)


def parse_rendered_prompt_sections(text: str) -> list[dict[str, object]]:
    matches = list(re.finditer(r"(?m)^## ([^\n]+)\n", str(text or "")))
    if not matches:
        return []
    sections: list[dict[str, object]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = match.group(1).strip()
        content = text[start:end].strip()
        if not content:
            continue
        section_id = title.split(".", 1)[0].strip().lower()
        sections.append(
            {
                "id": section_id,
                "title": title,
                "dynamic": not section_title_is_stable(title),
                "content": content,
            }
        )
    return sections


def cache_optimized_system_prompt(system_prompt: Any) -> Any:
    if not isinstance(system_prompt, str):
        return system_prompt
    sections = parse_rendered_prompt_sections(system_prompt)
    return sections or system_prompt
