from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
import mimetypes
from pathlib import Path
import re
from typing import Any

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - optional dependency in some environments
    Image = None
    ImageOps = None


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
EMBEDDED_USER_MESSAGE_PREFIX = "<open_somnia:user-message>"
EMBEDDED_USER_MESSAGE_SUFFIX = "</open_somnia:user-message>"
SUPPORTED_IMAGE_MEDIA_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
IMAGE_REFERENCE_BLOCK_TYPE = "image_reference"
IMAGE_DATA_URL_PATTERN = re.compile(
    r"^data:(image/(?:gif|jpeg|png|webp));base64,([a-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
MODEL_IMAGE_TARGET_BYTES = 120_000
MODEL_IMAGE_INLINE_MAX_BYTES_WITHOUT_PILLOW = 768_000
MODEL_IMAGE_HARD_MAX_BYTES = 1_500_000
MODEL_IMAGE_MAX_EDGE_STEPS = (1280, 1024, 768, 512)
MODEL_IMAGE_JPEG_QUALITIES = (85, 75, 65, 55)


def normalize_tool_importance(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_TOOL_IMPORTANCE:
        return normalized
    return None


def guess_image_media_type(path: str | Path, *, fallback: str | None = None) -> str | None:
    if fallback:
        normalized_fallback = str(fallback).strip().lower()
        if normalized_fallback in SUPPORTED_IMAGE_MEDIA_TYPES:
            return normalized_fallback
    guessed, _ = mimetypes.guess_type(str(path))
    normalized = str(guessed or "").strip().lower()
    if normalized in SUPPORTED_IMAGE_MEDIA_TYPES:
        return normalized
    return None


def parse_image_data_url(url: str) -> tuple[str, str] | None:
    match = IMAGE_DATA_URL_PATTERN.match(str(url or "").strip())
    if match is None:
        return None
    media_type = match.group(1).strip().lower()
    data = re.sub(r"\s+", "", match.group(2))
    if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES or not data:
        return None
    return media_type, data


def prepare_image_bytes_for_model(
    path: str | Path,
    *,
    fallback: str | None = None,
    target_bytes: int = MODEL_IMAGE_TARGET_BYTES,
) -> tuple[str, bytes]:
    image_path = Path(path)
    media_type = guess_image_media_type(image_path, fallback=fallback)
    if media_type is None:
        raise ValueError(f"Unsupported image format for model input: {image_path.name}")
    source_bytes = image_path.read_bytes()
    if len(source_bytes) <= target_bytes:
        return media_type, source_bytes
    if Image is None:
        if len(source_bytes) > MODEL_IMAGE_INLINE_MAX_BYTES_WITHOUT_PILLOW:
            raise ValueError(
                "Image is too large to inline safely without Pillow installed "
                f"({len(source_bytes)} bytes > {MODEL_IMAGE_INLINE_MAX_BYTES_WITHOUT_PILLOW} bytes). "
                "Install Pillow or shrink the image before reading it."
            )
        return media_type, source_bytes
    return _prepare_image_bytes_with_pillow(
        image_path,
        source_bytes=source_bytes,
        media_type=media_type,
        target_bytes=target_bytes,
    )


def _prepare_image_bytes_with_pillow(
    path: Path,
    *,
    source_bytes: bytes,
    media_type: str,
    target_bytes: int,
) -> tuple[str, bytes]:
    if Image is None:
        return media_type, source_bytes
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened) if ImageOps is not None else opened.copy()
        has_alpha = "A" in image.getbands()
        working = image.copy()
    best_media_type = media_type
    best_bytes = source_bytes
    for max_edge in MODEL_IMAGE_MAX_EDGE_STEPS:
        resized = _resize_image_to_max_edge(working, max_edge)
        for candidate_media_type, candidate_bytes in _encode_image_candidates(resized, preserve_alpha=has_alpha):
            if len(candidate_bytes) < len(best_bytes):
                best_media_type = candidate_media_type
                best_bytes = candidate_bytes
            if len(candidate_bytes) <= target_bytes:
                return candidate_media_type, candidate_bytes
    if len(best_bytes) > MODEL_IMAGE_HARD_MAX_BYTES:
        raise ValueError(
            f"Image is still too large after preprocessing ({len(best_bytes)} bytes). "
            "Shrink it before reading it."
        )
    return best_media_type, best_bytes


def _resize_image_to_max_edge(image: Any, max_edge: int):
    width, height = image.size
    current_max_edge = max(width, height)
    if current_max_edge <= max_edge:
        return image.copy()
    scale = max_edge / float(current_max_edge)
    new_size = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    resample = getattr(Image, "Resampling", Image).LANCZOS if Image is not None else None
    return image.resize(new_size, resample)


def _encode_image_candidates(image: Any, *, preserve_alpha: bool) -> list[tuple[str, bytes]]:
    candidates: list[tuple[str, bytes]] = []
    if preserve_alpha:
        png_buffer = io.BytesIO()
        image.save(png_buffer, format="PNG", optimize=True)
        candidates.append(("image/png", png_buffer.getvalue()))

    flattened = image
    if flattened.mode not in {"RGB", "L"}:
        if preserve_alpha:
            background = Image.new("RGB", image.size, (255, 255, 255))
            alpha_channel = image.getchannel("A") if "A" in image.getbands() else None
            if alpha_channel is not None:
                background.paste(image.convert("RGBA"), mask=alpha_channel)
                flattened = background
            else:
                flattened = image.convert("RGB")
        else:
            flattened = image.convert("RGB")

    if flattened.mode != "RGB":
        flattened = flattened.convert("RGB")

    for quality in MODEL_IMAGE_JPEG_QUALITIES:
        jpeg_buffer = io.BytesIO()
        flattened.save(
            jpeg_buffer,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
        candidates.append(("image/jpeg", jpeg_buffer.getvalue()))
    return candidates


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
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    raw_response: Any = None

    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self, tool_calls: list[ToolCall] | None = None) -> NormalizedMessage:
        selected_tool_calls = self.tool_calls if tool_calls is None else list(tool_calls)
        if self.content_blocks:
            selected_tool_calls_by_id = {tool_call.id: tool_call for tool_call in selected_tool_calls}
            blocks: list[dict[str, Any]] = []
            for block in self.content_blocks:
                block_type = str(block.get("type", "")).strip()
                if block_type == "tool_call":
                    block_id = str(block.get("id", "")).strip()
                    tool_call = selected_tool_calls_by_id.get(block_id)
                    if tool_call is None:
                        continue
                    tool_block = {
                        "type": "tool_call",
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "input": tool_call.input,
                    }
                    if tool_call.importance:
                        tool_block["importance"] = tool_call.importance
                    blocks.append(tool_block)
                    continue
                blocks.append(dict(block))
            if len(blocks) == 1 and blocks[0].get("type") == "text":
                return {"role": "assistant", "content": str(blocks[0].get("text", ""))}
            return {"role": "assistant", "content": blocks}
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


def make_user_content_message(content: Any) -> NormalizedMessage:
    return {"role": "user", "content": content}


def make_user_multimodal_message(text: str, blocks: list[dict[str, Any]]) -> NormalizedMessage:
    content_blocks: list[dict[str, Any]] = []
    normalized_text = str(text or "").strip()
    if normalized_text:
        content_blocks.append({"type": "text", "text": normalized_text})
    for block in blocks:
        if isinstance(block, dict):
            content_blocks.append(dict(block))
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})
    return make_user_content_message(content_blocks)


def make_image_reference_block(
    *,
    path: str | None = None,
    absolute_path: str | None = None,
    media_type: str | None = None,
    image_url: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": IMAGE_REFERENCE_BLOCK_TYPE}
    normalized_path = str(path or "").strip().replace("\\", "/")
    normalized_absolute_path = str(absolute_path or "").strip()
    normalized_media_type = str(media_type or "").strip().lower()
    normalized_image_url = str(image_url or "").strip()
    normalized_origin = str(origin or "").strip().lower()
    if normalized_path:
        block["path"] = normalized_path
    if normalized_absolute_path:
        block["absolute_path"] = normalized_absolute_path
    if normalized_media_type:
        block["media_type"] = normalized_media_type
    if normalized_image_url:
        block["image_url"] = normalized_image_url
    if normalized_origin:
        block["origin"] = normalized_origin
    return block


def image_source_block_to_reference(item: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
    block_type = str(item.get("type", "")).strip()
    if block_type == IMAGE_REFERENCE_BLOCK_TYPE:
        return dict(item)
    if block_type == "input_image":
        return make_image_reference_block(
            path=item.get("path"),
            absolute_path=item.get("absolute_path"),
            media_type=item.get("media_type"),
            origin=origin,
        )
    if block_type == "image_url":
        image_payload = item.get("image_url", {})
        if isinstance(image_payload, dict):
            url = image_payload.get("url")
        else:
            url = image_payload
        media_type = None
        parsed = parse_image_data_url(str(url or "").strip())
        if parsed is not None:
            media_type = parsed[0]
            url = ""
        return make_image_reference_block(
            media_type=media_type,
            image_url=str(url or "").strip(),
            origin=origin,
        )
    return dict(item)


def _image_reference_label(block: dict[str, Any]) -> str:
    image_url = str(block.get("image_url", "")).strip()
    if image_url and parse_image_data_url(image_url) is not None:
        image_url = ""
    path = str(block.get("path") or block.get("absolute_path") or image_url or "image").strip()
    media_type = str(block.get("media_type", "")).strip().lower()
    if media_type:
        return f"{path} ({media_type})"
    return path or "image"


def _image_reference_read_hint(block: dict[str, Any]) -> str:
    command_path = str(block.get("path") or "").strip().replace("\\", "/")
    if command_path:
        return f'Re-read with read_image(path="{command_path}") if needed.'
    return "Re-send the image if you need to inspect it again."


def render_image_reference_text(block: dict[str, Any], *, delivery: bool = False) -> str:
    prefix = "Image ready" if delivery else "Image reference"
    status = (
        "Visual data attached for the next model turn only."
        if delivery
        else "Visual data omitted from active context."
    )
    return f"[{prefix} | {_image_reference_label(block)}] {status} {_image_reference_read_hint(block)}"


def consume_ephemeral_image_blocks(messages: list[dict[str, Any]]) -> bool:
    changed = False
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            updated_content: list[Any] = []
            message_changed = False
            for item in content:
                if not isinstance(item, dict):
                    updated_content.append(item)
                    continue
                block_type = str(item.get("type", "")).strip()
                if block_type in {"input_image", "image_url"}:
                    updated_content.append(
                        image_source_block_to_reference(
                            item,
                            origin="user_input",
                        )
                    )
                    message_changed = True
                    continue
                if block_type == "tool_result":
                    updated_item = dict(item)
                    content_blocks = updated_item.get("content_blocks")
                    if isinstance(content_blocks, list):
                        updated_blocks: list[dict[str, Any]] = []
                        block_changed = False
                        image_references: list[dict[str, Any]] = []
                        for block in content_blocks:
                            if not isinstance(block, dict):
                                continue
                            nested_block_type = str(block.get("type", "")).strip()
                            if nested_block_type in {"input_image", "image_url"}:
                                reference_block = image_source_block_to_reference(
                                    block,
                                    origin="tool_result",
                                )
                                updated_blocks.append(reference_block)
                                image_references.append(reference_block)
                                block_changed = True
                            else:
                                updated_blocks.append(dict(block))
                        if block_changed:
                            if image_references:
                                reference_text = "\n".join(
                                    render_image_reference_text(reference_block)
                                    for reference_block in image_references
                                )
                                updated_item["content"] = reference_text
                                updated_item["tool_result_text"] = reference_text
                                updated_item["content_blocks"] = [
                                    {"type": "text", "text": reference_text},
                                    *image_references,
                                ]
                            else:
                                updated_item["content_blocks"] = updated_blocks
                            message_changed = True
                    updated_content.append(updated_item)
                    continue
                updated_content.append(dict(item))
            if message_changed:
                message["content"] = updated_content
                changed = True
    return changed


def make_tool_result_message(results: list[dict[str, Any]]) -> NormalizedMessage:
    return {"role": "user", "content": results}


def normalize_tool_result_content_blocks(value: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return normalized
    for item in value:
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type", "")).strip()
        if block_type == "text":
            normalized.append({"type": "text", "text": str(item.get("text", ""))})
            continue
        if block_type in {"image_url", "input_image", IMAGE_REFERENCE_BLOCK_TYPE}:
            block = dict(item)
            block["type"] = block_type
            normalized.append(block)
    return normalized


def active_tool_result_content_blocks(item: Any) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    stored_tool_result_text = item.get("tool_result_text")
    if isinstance(stored_tool_result_text, str) and stored_tool_result_text:
        current_content = str(item.get("content", ""))
        if current_content != stored_tool_result_text:
            return []
    blocks = normalize_tool_result_content_blocks(item.get("content_blocks"))
    if not any(str(block.get("type", "")).strip() in {"image_url", "input_image"} for block in blocks):
        return []
    return blocks


def make_tool_result_item(
    tool_call_id: str,
    output: Any,
    *,
    rendered_output: str,
    max_content_chars: int | None = None,
    raw_output: Any = None,
    log_id: str | None = None,
) -> dict[str, Any]:
    content = rendered_output
    tool_result_blocks: list[dict[str, Any]] = []
    explicit_tool_result_text = False
    if isinstance(output, dict):
        value = output.get("tool_result_text")
        if isinstance(value, str) and value.strip():
            content = value.strip()
            explicit_tool_result_text = True
        tool_result_blocks = normalize_tool_result_content_blocks(output.get("tool_result_content"))
    if max_content_chars is not None:
        content = content[: max(0, int(max_content_chars))]
    item = {
        "type": "tool_result",
        "tool_call_id": str(tool_call_id),
        "content": content,
    }
    if explicit_tool_result_text:
        item["tool_result_text"] = content
    if tool_result_blocks:
        item["content_blocks"] = tool_result_blocks
    if raw_output is not None:
        item["raw_output"] = raw_output
    if log_id is not None:
        item["log_id"] = str(log_id)
    if isinstance(output, dict):
        status = str(output.get("status", "")).strip().lower()
        if bool(output.get("is_error")) or status in {"error", "failed", "denied"}:
            item["is_error"] = True
    return item


def encode_embedded_user_message(message: NormalizedMessage) -> str:
    if str(message.get("role", "")).strip() != "user":
        raise ValueError("Only user messages can be embedded for transport.")
    return (
        EMBEDDED_USER_MESSAGE_PREFIX
        + json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        + EMBEDDED_USER_MESSAGE_SUFFIX
    )


def decode_embedded_user_message(value: Any) -> NormalizedMessage | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped.startswith(EMBEDDED_USER_MESSAGE_PREFIX) or not stripped.endswith(EMBEDDED_USER_MESSAGE_SUFFIX):
        return None
    payload = stripped[len(EMBEDDED_USER_MESSAGE_PREFIX) : -len(EMBEDDED_USER_MESSAGE_SUFFIX)]
    try:
        decoded = json.loads(payload)
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    content = decoded.get("content", "")
    if isinstance(content, list):
        normalized_content = [dict(item) if isinstance(item, dict) else item for item in content]
    else:
        normalized_content = content
    return {
        "role": "user",
        "content": normalized_content,
    }


def render_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == IMAGE_REFERENCE_BLOCK_TYPE:
                    parts.append(render_image_reference_text(item))
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
