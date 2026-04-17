from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


NormalizedMessage = dict[str, Any]
ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_ITALIC = "\x1b[3m"
ANSI_CODE = "\x1b[38;5;214m"
ANSI_HEADING = "\x1b[38;5;45m"
ANSI_QUOTE = "\x1b[38;5;110m"
ANSI_RULE = "\x1b[38;5;240m"
ANSI_CODE_BLOCK = "\x1b[38;5;180m"
HORIZONTAL_RULE = "\u2500" * 40
QUOTE_PREFIX = "\u2502 "
LIST_BULLET = "\u2022"
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
RULE_PATTERN = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
CODE_FENCE_PATTERN = re.compile(r"^\s*```")
QUOTE_PATTERN = re.compile(r"^\s*>\s?(.*)$")
LIST_PATTERN = re.compile(r"^(\s*)([-+*]|\d+\.)\s+(.*)$")
INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")
INLINE_BOLD_PATTERN = re.compile(r"(\*\*|__)(.+?)\1")
INLINE_ITALIC_PATTERN = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)")
ALLOWED_TOOL_IMPORTANCE = {"glance", "investigate", "foundation"}


def normalize_tool_importance(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_TOOL_IMPORTANCE:
        return normalized
    return None


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    importance: str | None = None


@dataclass(slots=True)
class AssistantTurn:
    stop_reason: str
    text_blocks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    raw_response: Any = None

    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self, tool_calls: list[ToolCall] | None = None) -> NormalizedMessage:
        selected_tool_calls = self.tool_calls if tool_calls is None else list(tool_calls)
        if not selected_tool_calls and len(self.text_blocks) == 1:
            return {"role": "assistant", "content": self.text_blocks[0]}
        blocks: list[dict[str, Any]] = []
        for text in self.text_blocks:
            blocks.append({"type": "text", "text": text})
        for tool_call in selected_tool_calls:
            block = {
                "type": "tool_call",
                "id": tool_call.id,
                "name": tool_call.name,
                "input": tool_call.input,
            }
            if tool_call.importance:
                block["importance"] = tool_call.importance
            blocks.append(block)
        return {"role": "assistant", "content": blocks}


def make_user_text_message(text: str) -> NormalizedMessage:
    return {"role": "user", "content": text}


def make_tool_result_message(results: list[dict[str, Any]]) -> NormalizedMessage:
    return {"role": "user", "content": results}


def render_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "tool_result":
                    parts.append(str(item.get("content", "")))
        return "\n".join(part for part in parts if part).strip()
    return str(content)


def _style(text: str, *codes: str, ansi: bool) -> str:
    if not ansi or not text:
        return text
    return "".join(codes) + text + ANSI_RESET


def _render_inline_markdown(text: str, *, ansi: bool) -> str:
    code_spans: list[str] = []

    def store_code(match: re.Match[str]) -> str:
        code_spans.append(match.group(1))
        return f"\0CODE{len(code_spans) - 1}\0"

    rendered = INLINE_CODE_PATTERN.sub(store_code, text)
    rendered = INLINE_BOLD_PATTERN.sub(
        lambda match: _style(match.group(2), ANSI_BOLD, ansi=ansi) if ansi else match.group(2),
        rendered,
    )
    rendered = INLINE_ITALIC_PATTERN.sub(
        lambda match: _style(match.group(1) or match.group(2) or "", ANSI_ITALIC, ansi=ansi)
        if ansi
        else (match.group(1) or match.group(2) or ""),
        rendered,
    )
    for index, code in enumerate(code_spans):
        placeholder = f"\0CODE{index}\0"
        replacement = _style(code, ANSI_CODE, ansi=ansi) if ansi else f"`{code}`"
        rendered = rendered.replace(placeholder, replacement)
    return rendered


def render_markdown_text(text: str, *, ansi: bool = False) -> str:
    if not text:
        return ""
    output: list[str] = []
    paragraph: list[str] = []
    in_code_block = False

    def flush_paragraph() -> None:
        if not paragraph:
            return
        combined = " ".join(part.strip() for part in paragraph if part.strip())
        if combined:
            output.append(_render_inline_markdown(combined, ansi=ansi))
        paragraph.clear()

    def push_blank_line() -> None:
        if output and output[-1] != "":
            output.append("")

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if in_code_block:
            if CODE_FENCE_PATTERN.match(line):
                in_code_block = False
                push_blank_line()
                continue
            code_line = f"    {line}" if line else ""
            output.append(_style(code_line, ANSI_CODE_BLOCK, ansi=ansi))
            continue

        if CODE_FENCE_PATTERN.match(line):
            flush_paragraph()
            push_blank_line()
            in_code_block = True
            continue
        if not line.strip():
            flush_paragraph()
            push_blank_line()
            continue
        if RULE_PATTERN.match(line):
            flush_paragraph()
            push_blank_line()
            output.append(_style(HORIZONTAL_RULE, ANSI_RULE, ansi=ansi))
            push_blank_line()
            continue

        heading_match = HEADING_PATTERN.match(line)
        if heading_match:
            flush_paragraph()
            push_blank_line()
            level = len(heading_match.group(1))
            heading_text = _render_inline_markdown(heading_match.group(2).strip(), ansi=ansi)
            if ansi:
                output.append(_style(heading_text, ANSI_BOLD, ANSI_HEADING, ansi=True))
            elif level == 1:
                output.extend([heading_text, "=" * max(len(heading_match.group(2).strip()), 1)])
            elif level == 2:
                output.extend([heading_text, "-" * max(len(heading_match.group(2).strip()), 1)])
            else:
                output.append(heading_text)
            push_blank_line()
            continue

        quote_match = QUOTE_PATTERN.match(line)
        if quote_match:
            flush_paragraph()
            quoted = _render_inline_markdown(quote_match.group(1).strip(), ansi=ansi)
            output.append(_style(f"{QUOTE_PREFIX}{quoted}", ANSI_QUOTE, ansi=ansi))
            continue

        list_match = LIST_PATTERN.match(line)
        if list_match:
            flush_paragraph()
            indent, marker, body = list_match.groups()
            prefix = marker if marker.endswith(".") else LIST_BULLET
            output.append(f"{indent}{prefix} {_render_inline_markdown(body.strip(), ansi=ansi)}")
            continue

        paragraph.append(line)

    flush_paragraph()
    while output and output[0] == "":
        output.pop(0)
    while output and output[-1] == "":
        output.pop()
    return "\n".join(output)


class MarkdownStreamRenderer:
    def __init__(self, *, ansi: bool = False) -> None:
        self.ansi = ansi
        self._pending = ""
        self._in_code_block = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._pending += text.replace("\r\n", "\n").replace("\r", "\n")
        output: list[str] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            output.append(self._render_line(line, terminated=True))
        return "".join(output)

    def finish(self) -> str:
        output = self.feed("")
        if self._pending:
            output += self._render_line(self._pending, terminated=False)
            self._pending = ""
        return output

    def _render_line(self, line: str, *, terminated: bool) -> str:
        rendered = self._render_markdown_line(line)
        if terminated:
            return rendered + "\n"
        return rendered

    def _render_markdown_line(self, line: str) -> str:
        if self._in_code_block:
            if CODE_FENCE_PATTERN.match(line):
                self._in_code_block = False
                return ""
            return _style(f"    {line}" if line else "", ANSI_CODE_BLOCK, ansi=self.ansi)

        if CODE_FENCE_PATTERN.match(line):
            self._in_code_block = True
            return ""
        if not line.strip():
            return ""
        if RULE_PATTERN.match(line):
            return _style(HORIZONTAL_RULE, ANSI_RULE, ansi=self.ansi)

        heading_match = HEADING_PATTERN.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = _render_inline_markdown(heading_match.group(2).strip(), ansi=self.ansi)
            if self.ansi:
                return _style(heading_text, ANSI_BOLD, ANSI_HEADING, ansi=True)
            if level == 1:
                raw_text = heading_match.group(2).strip()
                return f"{heading_text}\n{'=' * max(len(raw_text), 1)}"
            if level == 2:
                raw_text = heading_match.group(2).strip()
                return f"{heading_text}\n{'-' * max(len(raw_text), 1)}"
            return heading_text

        quote_match = QUOTE_PATTERN.match(line)
        if quote_match:
            quoted = _render_inline_markdown(quote_match.group(1).strip(), ansi=self.ansi)
            return _style(f"{QUOTE_PREFIX}{quoted}", ANSI_QUOTE, ansi=self.ansi)

        list_match = LIST_PATTERN.match(line)
        if list_match:
            indent, marker, body = list_match.groups()
            prefix = marker if marker.endswith(".") else LIST_BULLET
            return f"{indent}{prefix} {_render_inline_markdown(body.strip(), ansi=self.ansi)}"

        return _render_inline_markdown(line, ansi=self.ansi)


def render_message_content(content: Any, *, ansi: bool = False) -> str:
    return render_markdown_text(render_text_content(content), ansi=ansi)
