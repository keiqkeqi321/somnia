from __future__ import annotations

import base64
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from open_somnia.config.models import ProviderSettings
from open_somnia.providers.base import LLMProvider, ProviderError, StopChecker, TextCallback
from open_somnia.reasoning import openai_reasoning_payload
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import (
    IMAGE_REFERENCE_BLOCK_TYPE,
    active_tool_result_content_blocks,
    AssistantTurn,
    render_image_reference_text,
    ToolCall,
    normalize_tool_importance,
    prepare_image_bytes_for_model,
)

try:
    import tiktoken
except Exception:  # pragma: no cover - optional until dependencies are installed
    tiktoken = None


_CHAT_COMPLETION_KWARGS = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "max_tokens",
    "stream",
    "stream_options",
    "prompt_cache_key",
    "prompt_cache_retention",
}


def _dump_openai_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return json.loads(value.model_dump_json())


def _schema_to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _schema_to_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
    }


def _is_official_openai_base_url(base_url: str | None) -> bool:
    parsed = urlparse(str(base_url or "").strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "api.openai.com"


def _responses_api_url(base_url: str | None) -> str:
    normalized = str(base_url or "https://api.openai.com/v1").rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized[:-3]}/v1/responses"
    return f"{normalized}/responses"


def _openai_image_part(item: dict[str, Any]) -> dict[str, Any] | None:
    block_type = str(item.get("type", "")).strip()
    if block_type == "image_url":
        image_payload = item.get("image_url", {})
        if isinstance(image_payload, dict):
            url = str(image_payload.get("url", "")).strip()
            detail = str(image_payload.get("detail", "")).strip()
        else:
            url = str(image_payload).strip()
            detail = ""
        if not url:
            return None
        image_url: dict[str, Any] = {"url": url}
        if detail:
            image_url["detail"] = detail
        return {"type": "image_url", "image_url": image_url}
    if block_type != "input_image":
        return None
    image_path = str(item.get("absolute_path") or item.get("path") or "").strip()
    if not image_path:
        raise ProviderError("Image input is missing a file path.", retryable=False)
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        raise ProviderError(f"Image file not found: {image_path}", retryable=False)
    try:
        media_type, prepared_bytes = prepare_image_bytes_for_model(path, fallback=item.get("media_type"))
    except ValueError as exc:
        raise ProviderError(str(exc), retryable=False) from exc
    encoded = base64.b64encode(prepared_bytes).decode("ascii")
    detail = str(item.get("detail", "")).strip()
    image_url = {"url": f"data:{media_type};base64,{encoded}"}
    if detail:
        image_url["detail"] = detail
    return {"type": "image_url", "image_url": image_url}


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        text_parts: list[str] = []
        content_parts: list[dict[str, Any]] = []
        has_non_text_parts = False
        tool_results: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        tool_result_followup_blocks: list[dict[str, Any]] = []
        for item in content:
            if item["type"] == "text":
                text = str(item.get("text", ""))
                text_parts.append(text)
                content_parts.append({"type": "text", "text": text})
            elif item["type"] == IMAGE_REFERENCE_BLOCK_TYPE:
                text = render_image_reference_text(item)
                text_parts.append(text)
                content_parts.append({"type": "text", "text": text})
            elif item["type"] in {"image_url", "input_image"}:
                image_part = _openai_image_part(item)
                if image_part is not None:
                    content_parts.append(image_part)
                    has_non_text_parts = True
            elif item["type"] == "tool_result":
                tool_results.append(item)
                tool_result_followup_blocks.extend(active_tool_result_content_blocks(item))
            elif item["type"] == "tool_call":
                tool_calls.append(
                    {
                        "id": item["id"],
                        "type": "function",
                        "function": {
                            "name": item["name"],
                            "arguments": json.dumps(item.get("input", {}), ensure_ascii=False),
                        },
                    }
                )
        if role == "assistant":
            converted.append(
                {
                    "role": "assistant",
                    "content": "\n".join(part for part in text_parts if part) or "",
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                }
            )
            continue
        if content_parts:
            if has_non_text_parts:
                converted.append({"role": role, "content": content_parts})
            else:
                converted.append({"role": role, "content": "\n".join(text_parts)})
        for tool_result in tool_results:
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_result["tool_call_id"],
                    "content": str(tool_result.get("content", "")),
                }
            )
        followup_content = _openai_multimodal_content(tool_result_followup_blocks)
        if followup_content is not None:
            converted.append({"role": "user", "content": followup_content})
    return converted


def _openai_multimodal_content(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]] | None:
    if not blocks:
        return None
    text_parts: list[str] = []
    content_parts: list[dict[str, Any]] = []
    has_non_text_parts = False
    for item in blocks:
        block_type = str(item.get("type", "")).strip()
        if block_type == "text":
            text = str(item.get("text", ""))
            text_parts.append(text)
            content_parts.append({"type": "text", "text": text})
            continue
        if block_type == IMAGE_REFERENCE_BLOCK_TYPE:
            text = render_image_reference_text(item)
            text_parts.append(text)
            content_parts.append({"type": "text", "text": text})
            continue
        if block_type not in {"image_url", "input_image"}:
            continue
        image_part = _openai_image_part(item)
        if image_part is not None:
            content_parts.append(image_part)
            has_non_text_parts = True
    if not content_parts:
        return None
    if has_non_text_parts:
        return content_parts
    return "\n".join(part for part in text_parts if part)


class _ThinkTagSplitter:
    def __init__(self) -> None:
        self._buffer = ""
        self._in_thinking = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        if not text:
            return []
        self._buffer += text
        parts: list[tuple[str, str]] = []
        while self._buffer:
            if self._in_thinking:
                close_index = self._buffer.find("</think>")
                if close_index < 0:
                    keep = min(len("</think>") - 1, len(self._buffer))
                    emit_length = len(self._buffer) - keep
                    if emit_length <= 0:
                        break
                    parts.append(("thinking", self._buffer[:emit_length]))
                    self._buffer = self._buffer[emit_length:]
                    break
                if close_index:
                    parts.append(("thinking", self._buffer[:close_index]))
                self._buffer = self._buffer[close_index + len("</think>") :]
                self._in_thinking = False
                continue
            open_index = self._buffer.find("<think>")
            if open_index < 0:
                keep = min(len("<think>") - 1, len(self._buffer))
                emit_length = len(self._buffer) - keep
                if emit_length <= 0:
                    break
                parts.append(("text", self._buffer[:emit_length]))
                self._buffer = self._buffer[emit_length:]
                break
            if open_index:
                parts.append(("text", self._buffer[:open_index]))
            self._buffer = self._buffer[open_index + len("<think>") :]
            self._in_thinking = True
        return parts

    def flush(self) -> list[tuple[str, str]]:
        if not self._buffer:
            return []
        kind = "thinking" if self._in_thinking else "text"
        text = self._buffer
        self._buffer = ""
        return [(kind, text)]


def _responses_content_from_openai_content(content: Any, *, role: str) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "")).strip()
        if item_type == "text":
            text = str(item.get("text", ""))
            parts.append({"type": "output_text" if role == "assistant" else "input_text", "text": text})
            continue
        if item_type == "image_url":
            image_payload = item.get("image_url", {})
            image_url = str(image_payload.get("url", "") if isinstance(image_payload, dict) else image_payload).strip()
            if image_url:
                part: dict[str, Any] = {"type": "input_image", "image_url": image_url}
                if isinstance(image_payload, dict) and image_payload.get("detail"):
                    part["detail"] = image_payload["detail"]
                parts.append(part)
    if not parts:
        return ""
    return parts


def _to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    response_input: list[dict[str, Any]] = []
    for message in _to_openai_messages(messages):
        role = str(message.get("role", "")).strip()
        if role == "tool":
            response_input.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id", "")),
                    "output": str(message.get("content", "")),
                }
            )
            continue
        if role == "assistant":
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                response_input.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                converted = _responses_content_from_openai_content(content, role="assistant")
                if converted:
                    response_input.append({"role": "assistant", "content": converted})
            for tool_call in message.get("tool_calls", []) or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function", {})
                response_input.append(
                    {
                        "type": "function_call",
                        "call_id": str(tool_call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "arguments": str(function.get("arguments", "{}") or "{}"),
                        "status": "completed",
                    }
                )
            continue
        if role in {"user", "system", "developer"}:
            response_input.append(
                {
                    "role": role,
                    "content": _responses_content_from_openai_content(message.get("content", ""), role=role),
                }
            )
    return response_input


def _encoding_for_openai_model(model: str):
    if tiktoken is None:
        raise ProviderError("tiktoken is not installed.")
    candidates = [model.strip()]
    if "/" in model:
        candidates.append(model.split("/", 1)[1].strip())
    if ":" in model:
        candidates.append(model.split(":", 1)[0].strip())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return tiktoken.encoding_for_model(candidate)
        except KeyError:
            continue
    lowered = model.strip().lower()
    if any(token in lowered for token in ("gpt-4.1", "gpt-5", "o1", "o3", "o4")):
        return tiktoken.get_encoding("o200k_base")
    return tiktoken.get_encoding("cl100k_base")


def _parse_error_payload(details: str) -> dict[str, Any]:
    try:
        payload = json.loads(details)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_overloaded_error(status_code: int, details: str) -> bool:
    payload = _parse_error_payload(details)
    error_payload = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
    error_code = str(error_payload.get("code", "")).strip().lower()
    message = str(error_payload.get("message", "")).strip().lower()
    detail_text = details.strip().lower()
    overload_markers = (
        "访问量过大",
        "too many requests",
        "rate limit",
        "overload",
        "overloaded",
        "capacity",
        "busy",
    )
    if status_code == 429:
        return True
    if error_code in {"1305", "rate_limit_exceeded", "overloaded"}:
        return True
    return any(marker in message or marker in detail_text for marker in overload_markers)


def _is_forbidden_like_error(status_code: int, details: str) -> bool:
    payload = _parse_error_payload(details)
    error_payload = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
    error_type = str(error_payload.get("type", "")).strip().lower()
    error_code = str(error_payload.get("code", "")).strip().lower()
    message = str(error_payload.get("message", "")).strip().lower()
    detail_text = details.strip().lower()
    forbidden_markers = (
        "forbidden",
        "access denied",
        "access forbidden",
        "unauthorized",
        "not allowed",
        "permission denied",
        "contact administrator",
        "policy",
    )
    if status_code in {401, 403}:
        return True
    if error_type in {"authentication_error", "permission_error", "access_error", "upstream_error"}:
        if any(marker in message or marker in detail_text for marker in forbidden_markers):
            return True
    if error_code in {"forbidden", "access_denied", "permission_denied", "unauthorized"}:
        return True
    return any(marker in message or marker in detail_text for marker in forbidden_markers)


def _openai_exception_retryable(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code in {408, 409, 429}:
            return True
        if status_code >= 500:
            return True
        if 400 <= status_code < 500:
            return False

    type_name = type(exc).__name__.lower()
    message = str(exc).strip().lower()
    retryable_markers = (
        "timeout",
        "timed out",
        "connection",
        "connect",
        "temporar",
        "temporary",
        "network",
        "service unavailable",
        "internal server",
        "overloaded",
        "rate limit",
        "server disconnected",
    )
    non_retryable_markers = (
        "authentication",
        "auth",
        "permission",
        "forbidden",
        "unauthorized",
        "invalid",
        "bad request",
        "not found",
        "unprocessable",
    )
    if any(marker in type_name or marker in message for marker in non_retryable_markers):
        return False
    if any(marker in type_name or marker in message for marker in retryable_markers):
        return True
    return True


def _wrap_openai_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(
        f"OpenAI request failed: {exc}",
        retryable=_openai_exception_retryable(exc),
    )


def _parse_tool_call_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None]:
    arguments_text = str(raw_arguments or "{}")
    try:
        arguments = json.loads(arguments_text)
    except Exception as exc:
        return {}, f"Tool call arguments were invalid JSON and were ignored: {exc}"
    if not isinstance(arguments, dict):
        return {}, "Tool call arguments were not a JSON object and were ignored."
    return arguments, None


def _first_openai_choice(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("OpenAI response did not include any choices.", retryable=False)
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderError("OpenAI response choice was not an object.", retryable=False)
    return choice


class OpenAIProvider(LLMProvider):
    def __init__(self, settings: ProviderSettings):
        self.settings = settings

    def _client(self) -> OpenAI:
        kwargs: dict[str, Any] = {
            "api_key": self.settings.api_key,
            "timeout": self.settings.timeout_seconds,
        }
        if self.settings.base_url:
            kwargs["base_url"] = self.settings.base_url
        if self.settings.organization:
            kwargs["organization"] = self.settings.organization
        return OpenAI(**kwargs)

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        encoding = _encoding_for_openai_model(self.settings.model)
        payload = self.debug_request_payload(system_prompt, messages, tools, self.settings.max_tokens, stream=False)["body"]
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return len(encoding.encode(serialized))

    def token_counter_name(self) -> str:
        return "tiktoken"

    def _extract_usage(self, body: dict[str, Any]) -> dict[str, Any] | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + output_tokens))
        cache_hit_tokens = int(usage.get("prompt_cache_hit_tokens") or 0)
        cache_miss_tokens = int(usage.get("prompt_cache_miss_tokens") or 0)
        prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        if cache_hit_tokens == 0 and isinstance(prompt_details, dict):
            cache_hit_tokens = int(prompt_details.get("cached_tokens") or 0)
        if cache_miss_tokens == 0 and cache_hit_tokens and prompt_tokens > cache_hit_tokens:
            cache_miss_tokens = prompt_tokens - cache_hit_tokens
        input_tokens = cache_miss_tokens if cache_hit_tokens else prompt_tokens
        result = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "source": "provider",
        }
        if cache_hit_tokens:
            result["cache_read_input_tokens"] = cache_hit_tokens
        return result

    def _responses_reasoning_payload(self) -> dict[str, Any]:
        reasoning_payload = openai_reasoning_payload(
            model=self.settings.model,
            reasoning_level=getattr(self.settings, "reasoning_level", None),
            supports_reasoning=getattr(self.settings, "supports_reasoning", None),
        )
        reasoning = reasoning_payload.get("reasoning")
        if isinstance(reasoning, dict):
            return {"reasoning": {**reasoning, "summary": "auto"}}
        return {}

    def _use_responses_api(self) -> bool:
        if not _is_official_openai_base_url(self.settings.base_url):
            return False
        return bool(self._responses_reasoning_payload())

    def debug_request_payload(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        reasoning_payload = openai_reasoning_payload(
            model=self.settings.model,
            reasoning_level=getattr(self.settings, "reasoning_level", None),
            supports_reasoning=getattr(self.settings, "supports_reasoning", None),
        )
        prompt_cache_payload = self._prompt_cache_payload()
        if self._use_responses_api():
            return {
                "url": _responses_api_url(self.settings.base_url),
                "body": {
                    "model": self.settings.model,
                    "instructions": system_prompt,
                    "input": _to_responses_input(messages),
                    "tools": [_schema_to_responses_tool(tool) for tool in tools],
                    "tool_choice": "auto",
                    "max_output_tokens": max_tokens,
                    "stream": stream,
                    **prompt_cache_payload,
                    **self._responses_reasoning_payload(),
                },
            }
        body = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system_prompt}] + _to_openai_messages(messages),
            "tools": [_schema_to_openai_tool(tool) for tool in tools],
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "stream": stream,
            **prompt_cache_payload,
            **reasoning_payload,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        return {
            "url": f"{self.settings.base_url.rstrip('/')}/chat/completions",
            "body": body,
        }

    def _prompt_cache_payload(self) -> dict[str, Any]:
        if not _is_official_openai_base_url(self.settings.base_url):
            return {}
        payload: dict[str, Any] = {}
        prompt_cache_key = str(getattr(self.settings, "prompt_cache_key", "") or "").strip()
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        prompt_cache_retention = str(getattr(self.settings, "prompt_cache_retention", "") or "").strip()
        if prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention
        return payload

    def _chat_completion_kwargs(self, payload: dict[str, Any]) -> dict[str, Any]:
        kwargs = {key: value for key, value in payload.items() if key in _CHAT_COMPLETION_KWARGS}
        extra_body = {key: value for key, value in payload.items() if key not in _CHAT_COMPLETION_KWARGS}
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs

    def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        text_callback: TextCallback | None = None,
        thinking_callback=None,
        stop_checker: StopChecker | None = None,
    ) -> AssistantTurn:
        should_stream = text_callback is not None or stop_checker is not None
        debug_payload = self.debug_request_payload(
            system_prompt,
            messages,
            tools,
            max_tokens,
            stream=should_stream,
        )
        payload = debug_payload["body"]
        try:
            client = self._client()
            if self._use_responses_api():
                response = client.responses.create(**payload)
                if should_stream:
                    body = self._read_responses_streaming_response(
                        response,
                        text_callback,
                        thinking_callback=thinking_callback,
                        stop_checker=stop_checker,
                    )
                else:
                    body = _dump_openai_object(response)
                    self._emit_responses_reasoning_summary(body, thinking_callback)
            else:
                response = client.chat.completions.create(**self._chat_completion_kwargs(payload))
                if should_stream:
                    body = self._read_streaming_response(
                        response,
                        text_callback,
                        thinking_callback=thinking_callback,
                        stop_checker=stop_checker,
                    )
                else:
                    body = _dump_openai_object(response)
        except APIStatusError as exc:
            details = str(getattr(exc, "body", None) or exc.response.text or exc)
            retryable = (
                exc.status_code >= 500
                and not _is_overloaded_error(exc.status_code, details)
                and not _is_forbidden_like_error(exc.status_code, details)
            )
            raise ProviderError(
                f"OpenAI request failed: {exc.status_code} {details}",
                retryable=retryable,
            ) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            retryable = isinstance(getattr(exc, "__cause__", None), TimeoutError | socket.timeout) or "timed out" in str(exc).lower()
            raise ProviderError(f"OpenAI request failed: {exc}", retryable=retryable) from exc
        except Exception as exc:
            raise _wrap_openai_exception(exc) from exc

        if self._use_responses_api():
            return self._assistant_turn_from_responses_body(body)

        choice = _first_openai_choice(body)
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError("OpenAI response choice did not include a message.", retryable=False)
        text_blocks: list[str] = []
        content_blocks: list[dict[str, Any]] = []
        thinking_streamed = bool(body.get("_thinking_streamed"))
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            content_blocks.append({"type": "thinking", "thinking": reasoning_content})
            if callable(thinking_callback) and not thinking_streamed:
                thinking_callback({"event": "delta", "type": "reasoning_content", "delta": reasoning_content})
        content = message.get("content")
        if isinstance(content, str) and content:
            text_blocks.append(content)
            content_blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    text = item.get("text", "")
                    text_blocks.append(text)
                    content_blocks.append({"type": "text", "text": text})
        tool_calls = []
        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            raw_tool_calls = []
        tool_call_warnings: list[str] = []
        for tool_call in raw_tool_calls:
            arguments, warning = _parse_tool_call_arguments(tool_call["function"].get("arguments"))
            if warning:
                tool_call_warnings.append(warning)
            importance = normalize_tool_importance(arguments.pop("importance", None))
            tool_calls.append(
                ToolCall(
                    id=tool_call["id"],
                    name=tool_call["function"]["name"],
                    input=arguments,
                    importance=importance,
                )
            )
            tool_call_block = {
                "type": "tool_call",
                "id": tool_calls[-1].id,
                "name": tool_calls[-1].name,
                "input": tool_calls[-1].input,
            }
            if tool_calls[-1].importance:
                tool_call_block["importance"] = tool_calls[-1].importance
            content_blocks.append(tool_call_block)
        if tool_call_warnings:
            warning_text = "\n".join(tool_call_warnings)
            text_blocks.append(warning_text)
            content_blocks.append({"type": "text", "text": warning_text})
        stop_reason = choice.get("finish_reason") or "stop"
        if stop_reason == "tool_calls":
            stop_reason = "tool_use"
        elif stop_reason == "stop":
            stop_reason = "end_turn"
        return AssistantTurn(
            stop_reason=stop_reason,
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            content_blocks=content_blocks,
            usage=self._extract_usage(body),
            raw_response=body,
        )

    def _assistant_turn_from_responses_body(self, body: dict[str, Any]) -> AssistantTurn:
        output = body.get("output")
        if not isinstance(output, list):
            raise ProviderError("OpenAI Responses API response did not include output items.", retryable=False)
        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).strip()
            if item_type == "message":
                content = item.get("content", [])
                if isinstance(content, str) and content:
                    text_blocks.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        part_type = str(part.get("type", "")).strip()
                        if part_type in {"output_text", "text"} and str(part.get("text", "")).strip():
                            text_blocks.append(str(part.get("text", "")))
                continue
            if item_type == "function_call":
                arguments = str(item.get("arguments", "{}") or "{}")
                try:
                    parsed_arguments = json.loads(arguments)
                except Exception:
                    parsed_arguments = {}
                if not isinstance(parsed_arguments, dict):
                    parsed_arguments = {}
                importance = normalize_tool_importance(parsed_arguments.pop("importance", None))
                tool_calls.append(
                    ToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name", "")),
                        input=parsed_arguments,
                        importance=importance,
                    )
                )
        return AssistantTurn(
            stop_reason="tool_use" if tool_calls else "end_turn",
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            usage=self._extract_usage(body),
            raw_response=body,
        )

    def _extract_responses_reasoning_summary(self, body: dict[str, Any]) -> str:
        output = body.get("output")
        if not isinstance(output, list):
            return ""
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or str(item.get("type", "")).strip() != "reasoning":
                continue
            summary = item.get("summary", [])
            if isinstance(summary, str):
                parts.append(summary)
                continue
            if not isinstance(summary, list):
                continue
            for part in summary:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    text = str(part.get("text", "") or part.get("summary_text", "") or "")
                    if text:
                        parts.append(text)
        return "".join(parts)

    def _emit_responses_reasoning_summary(self, body: dict[str, Any], thinking_callback) -> None:
        summary = self._extract_responses_reasoning_summary(body)
        if summary and callable(thinking_callback):
            thinking_callback({"event": "delta", "type": "reasoning_summary", "delta": summary})

    def _read_streaming_response(
        self,
        response,
        text_callback: TextCallback | None,
        *,
        thinking_callback=None,
        stop_checker: StopChecker | None = None,
    ) -> dict[str, Any]:
        aggregated_message: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": []}
        thinking_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        think_splitter = _ThinkTagSplitter()
        finish_reason = "stop"
        usage: dict[str, Any] | None = None

        def emit_text(text: str) -> None:
            aggregated_message["content"] += text
            if text_callback is not None:
                text_callback(text)

        def emit_thinking(thinking: str, thinking_type: str) -> None:
            thinking_parts.append(thinking)
            if callable(thinking_callback):
                thinking_callback({"event": "delta", "type": thinking_type, "delta": thinking})

        def emit_content_delta(text: str) -> None:
            for kind, part in think_splitter.feed(text):
                if not part:
                    continue
                if kind == "thinking":
                    emit_thinking(part, "think_tag")
                else:
                    emit_text(part)

        for raw_event in response:
            if stop_checker is not None and stop_checker():
                raise TurnInterrupted("Interrupted by user.")
            event = _dump_openai_object(raw_event)
            event_usage = event.get("usage")
            if isinstance(event_usage, dict):
                usage = event_usage
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta", {})
            finish_reason = choice.get("finish_reason") or finish_reason

            reasoning_content = delta.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                emit_thinking(reasoning_content, "reasoning_content")

            content = delta.get("content")
            if isinstance(content, str) and content:
                emit_content_delta(content)
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            emit_content_delta(text)

            raw_tool_deltas = delta.get("tool_calls") or []
            if not isinstance(raw_tool_deltas, list):
                raw_tool_deltas = []
            for tool_delta in raw_tool_deltas:
                index = int(tool_delta.get("index", 0))
                current = tool_calls_by_index.setdefault(
                    index,
                    {
                        "id": tool_delta.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tool_delta.get("id"):
                    current["id"] = tool_delta["id"]
                function_delta = tool_delta.get("function", {})
                if function_delta.get("name"):
                    current["function"]["name"] = function_delta["name"]
                if function_delta.get("arguments"):
                    current["function"]["arguments"] += function_delta["arguments"]

        for kind, part in think_splitter.flush():
            if not part:
                continue
            if kind == "thinking":
                emit_thinking(part, "think_tag")
            else:
                emit_text(part)
        if thinking_parts:
            aggregated_message["reasoning_content"] = "".join(thinking_parts)
        aggregated_message["tool_calls"] = [tool_calls_by_index[index] for index in sorted(tool_calls_by_index)]
        body = {
            "_thinking_streamed": bool(thinking_parts),
            "choices": [
                {
                    "message": aggregated_message,
                    "finish_reason": finish_reason,
                }
            ]
        }
        if usage is not None:
            body["usage"] = usage
        return body

    def _read_responses_streaming_response(
        self,
        response,
        text_callback: TextCallback | None,
        *,
        thinking_callback=None,
        stop_checker: StopChecker | None = None,
    ) -> dict[str, Any]:
        completed_body: dict[str, Any] | None = None
        output_items: list[dict[str, Any]] = []

        def dispatch_event(event_name: str, data_text: str) -> None:
            nonlocal completed_body
            if not data_text or data_text == "[DONE]":
                return
            try:
                event = json.loads(data_text)
            except Exception:
                return
            event_type = str(event_name or event.get("type", "")).strip()
            if event_type == "response.reasoning_summary_text.delta":
                delta = str(event.get("delta", "") or "")
                if delta and callable(thinking_callback):
                    thinking_callback({"event": "delta", "type": "reasoning_summary", "delta": delta})
                return
            if event_type == "response.output_text.delta":
                delta = str(event.get("delta", "") or "")
                if delta and text_callback is not None:
                    text_callback(delta)
                return
            if event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    output_items.append(item)
                return
            if event_type == "response.completed":
                response_body = event.get("response")
                if isinstance(response_body, dict):
                    completed_body = response_body

        for raw_event in response:
            if stop_checker is not None and stop_checker():
                raise TurnInterrupted("Interrupted by user.")
            event = _dump_openai_object(raw_event)
            dispatch_event(str(event.get("type", "")), json.dumps(event, ensure_ascii=False))
        if completed_body is not None:
            return completed_body
        return {"output": output_items}
