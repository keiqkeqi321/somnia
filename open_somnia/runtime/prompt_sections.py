from __future__ import annotations

from dataclasses import dataclass


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
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class PromptBundle:
    sections: tuple[PromptSection, ...]

    def render(self) -> str:
        return "\n\n".join(rendered for section in self.sections if (rendered := section.render()))

    def to_payload(self) -> list[dict[str, object]]:
        return [section.to_payload() for section in self.sections if section.content.strip()]
