from __future__ import annotations

import base64
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from open_somnia.config.models import ProviderSettings
from open_somnia.providers.base import LLMProvider, ProviderError, StopChecker, TextCallback, ThinkingCallback
from open_somnia.reasoning import anthropic_reasoning_payload
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import (
    IMAGE_REFERENCE_BLOCK_TYPE,
    active_tool_result_content_blocks,
    AssistantTurn,
    render_image_reference_text,
    ToolCall,
    normalize_tool_importance,
    parse_image_data_url,
    prepare_image_bytes_for_model,
)
from open_somnia.runtime.prompt_sections import parse_rendered_prompt_sections


def _anthropic_image_block(item: dict[str, Any]) -> dict[str, Any] | None:
    block_type = str(item.get("type", "")).strip()
    if block_type == "image_url":
        image_payload = item.get("image_url", {})
        if isinstance(image_payload, dict):
            url = str(image_payload.get("url", "")).strip()
        else:
            url = str(image_payload).strip()
        parsed = parse_image_data_url(url)
        if parsed is None:
            raise ProviderError(
                "Anthropic-compatible vision input requires embedded data URLs or local image files.",
                retryable=False,
            )
        media_type, data = parsed
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }
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
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(prepared_bytes).decode("ascii"),
        },
    }


def _cache_control() -> dict[str, str]:
    return {"type": "ephemeral"}


def _with_cache_control(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "cache_control": _cache_control()}


def _to_anthropic_system(system_prompt: Any) -> str | list[dict[str, Any]]:
    if isinstance(system_prompt, list):
        blocks: list[dict[str, Any]] = []
        last_stable_index: int | None = None
        for item in system_prompt:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            title = str(item.get("title", "") or "").strip()
            text = f"## {title}\n{content}" if title else content
            blocks.append({"type": "text", "text": text})
            if not bool(item.get("dynamic", False)):
                last_stable_index = len(blocks) - 1
        if blocks and last_stable_index is not None:
            blocks[last_stable_index] = _with_cache_control(blocks[last_stable_index])
        return blocks or ""
    text = str(system_prompt or "")
    if not text.strip():
        return ""
    rendered_sections = parse_rendered_prompt_sections(text)
    if rendered_sections:
        return _to_anthropic_system(rendered_sections)
    return [_with_cache_control({"type": "text", "text": text})]


def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Anthropic cache prefixes are built tools -> system -> messages, so the
    # last tool always carries a breakpoint: tool definitions stay cache-read
    # even when later system sections or messages change. Total breakpoints per
    # request: tools tier + stable-system tier + last message = 3 of the max 4.
    converted = [dict(tool) for tool in tools]
    if converted:
        converted[-1] = _with_cache_control(converted[-1])
    return converted


def _add_message_cache_control(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return messages
    converted = [dict(message) for message in messages]
    target_index: int | None = None
    for index in range(len(converted) - 1, -1, -1):
        if bool(converted[index].get("transient")):
            continue
        target_index = index
        break
    if target_index is None:
        return [
            {key: value for key, value in message.items() if key != "transient"}
            for message in converted
        ]
    last = dict(converted[target_index])
    content = last.get("content")
    if isinstance(content, str):
        if content.strip():
            last["content"] = [_with_cache_control({"type": "text", "text": content})]
    elif isinstance(content, list) and content:
        content_blocks = [dict(block) if isinstance(block, dict) else block for block in content]
        for index in range(len(content_blocks) - 1, -1, -1):
            if isinstance(content_blocks[index], dict):
                content_blocks[index] = _with_cache_control(content_blocks[index])
                last["content"] = content_blocks
                break
    converted[target_index] = last
    return [
        {key: value for key, value in message.items() if key != "transient"}
        for message in converted
    ]


def _to_anthropic_messages(messages: list[dict[str, Any]], *, cache_last_message: bool = False) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            converted_message = {"role": role, "content": content}
            if bool(message.get("transient")):
                converted_message["transient"] = True
            converted.append(converted_message)
            continue
        blocks: list[dict[str, Any]] = []
        for item in content:
            if item["type"] == "text":
                blocks.append({"type": "text", "text": str(item.get("text", ""))})
            elif item["type"] == IMAGE_REFERENCE_BLOCK_TYPE:
                blocks.append({"type": "text", "text": render_image_reference_text(item)})
            elif item["type"] in {"image_url", "input_image"}:
                image_block = _anthropic_image_block(item)
                if image_block is not None:
                    blocks.append(image_block)
            elif item["type"] == "thinking":
                if role != "assistant":
                    continue
                thinking_block = {
                    "type": "thinking",
                    "thinking": str(item.get("thinking", "")),
                }
                signature = str(item.get("signature", "") or "")
                if signature:
                    thinking_block["signature"] = signature
                blocks.append(thinking_block)
            elif item["type"] == "redacted_thinking":
                if role != "assistant":
                    continue
                blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": item.get("data"),
                    }
                )
            elif item["type"] == "thinking_log":
                continue
            elif item["type"] == "tool_call":
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": item["id"],
                        "name": item["name"],
                        "input": item.get("input", {}),
                    }
                )
            elif item["type"] == "tool_result":
                tool_result_content = _anthropic_tool_result_content(item)
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": item["tool_call_id"],
                        "content": tool_result_content or str(item.get("content", "")),
                        "is_error": bool(item.get("is_error", False)),
                    }
                )
        converted_message = {"role": role, "content": blocks}
        if bool(message.get("transient")):
            converted_message["transient"] = True
        converted.append(converted_message)
    if cache_last_message:
        return _add_message_cache_control(converted)
    return converted


def _anthropic_tool_result_content(value: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in active_tool_result_content_blocks(value):
        block_type = str(item.get("type", "")).strip()
        if block_type == "text":
            blocks.append({"type": "text", "text": str(item.get("text", ""))})
            continue
        if block_type == IMAGE_REFERENCE_BLOCK_TYPE:
            blocks.append({"type": "text", "text": render_image_reference_text(item)})
            continue
        if block_type not in {"image_url", "input_image"}:
            continue
        image_block = _anthropic_image_block(item)
        if image_block is not None:
            blocks.append(image_block)
    return blocks


def _anthropic_exception_retryable(exc: Exception) -> bool:
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
        "apiconnectionerror",
        "apitimeouterror",
        "internalservererror",
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
        "ratelimiterror",
        "permissiondeniederror",
        "authenticationerror",
        "badrequesterror",
        "notfounderror",
    )
    if any(marker in type_name or marker in message for marker in non_retryable_markers):
        return False
    if any(marker in type_name or marker in message for marker in retryable_markers):
        return True
    return True


def _anthropic_exception_kind(exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code in {401, 403}:
            return "auth"
        if status_code == 429:
            return "quota"
        if status_code == 408:
            return "timeout"
        if status_code in {400, 404, 422}:
            return "model"

    type_name = type(exc).__name__.lower()
    if "authenticationerror" in type_name or "permissiondeniederror" in type_name:
        return "auth"
    if "ratelimiterror" in type_name:
        return "quota"
    if "apitimeouterror" in type_name:
        return "timeout"
    if "badrequesterror" in type_name or "notfounderror" in type_name:
        return "model"
    message = str(exc).lower()
    if "timed out" in message or "timeout" in message:
        return "timeout"
    return "other"


def _wrap_anthropic_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(
        f"Anthropic request failed: {exc}",
        retryable=_anthropic_exception_retryable(exc),
        kind=_anthropic_exception_kind(exc),
    )


def _is_stream_json_parse_error(exc: Exception) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    type_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "jsondecodeerror" in type_name
        or "unterminated string" in message
        or "expecting value" in message
        or "invalid control character" in message
    )


@lru_cache(maxsize=8)
def _shared_anthropic_client(api_key: str | None, base_url: str | None) -> Anthropic:
    # SDK client construction pays httpx/SSL setup that can take seconds on
    # slow machines; the client is model-independent, so share one per
    # (api_key, base_url) endpoint across provider instances.
    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return Anthropic(**kwargs)


class AnthropicProvider(LLMProvider):
    def __init__(self, settings: ProviderSettings):
        self.client = _shared_anthropic_client(settings.api_key, settings.base_url)
        self.settings = settings

    def count_tokens(
        self,
        system_prompt: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        system = _to_anthropic_system(system_prompt)
        response = self.client.messages.count_tokens(
            model=self.settings.model,
            system=system,
            messages=_to_anthropic_messages(messages, cache_last_message=True),
            tools=_to_anthropic_tools(tools),
            timeout=self.settings.timeout_seconds,
        )
        return int(response.input_tokens)

    def token_counter_name(self) -> str:
        return "anthropic_native"

    def _extract_usage(self, response: Any) -> dict[str, Any] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read_input_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation_input_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        total_tokens = input_tokens + output_tokens
        result = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "source": "provider",
        }
        if cache_read_input_tokens or cache_creation_input_tokens:
            result["cache_read_input_tokens"] = cache_read_input_tokens
            result["cache_creation_input_tokens"] = cache_creation_input_tokens
        return result

    def debug_request_payload(
        self,
        system_prompt: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        system = _to_anthropic_system(system_prompt)
        payload = {
            "model": self.settings.model,
            "system": system,
            "messages": _to_anthropic_messages(messages, cache_last_message=True),
            "tools": _to_anthropic_tools(tools),
            "max_tokens": max_tokens,
            "stream": stream,
        }
        payload.update(
            anthropic_reasoning_payload(
                model=self.settings.model,
                reasoning_level=getattr(self.settings, "reasoning_level", None),
                max_tokens=max_tokens,
                supports_reasoning=getattr(self.settings, "supports_reasoning", None),
                supports_adaptive_reasoning=getattr(self.settings, "supports_adaptive_reasoning", None),
            )
        )
        return payload

    def complete(
        self,
        system_prompt: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        text_callback: TextCallback | None = None,
        thinking_callback: ThinkingCallback | None = None,
        stop_checker: StopChecker | None = None,
    ) -> AssistantTurn:
        request_kwargs = self.debug_request_payload(
            system_prompt,
            messages,
            tools,
            max_tokens,
            stream=text_callback is not None or stop_checker is not None,
        )
        request_kwargs.pop("stream", None)
        streamed_thinking_delta = False
        streamed_redacted_thinking = False
        streamed_text_delta = False
        try:
            if text_callback is None and stop_checker is None:
                response = self.client.messages.create(**request_kwargs)
            else:
                with self.client.messages.stream(**request_kwargs) as stream:
                    if stop_checker is not None and stop_checker():
                        raise TurnInterrupted("Interrupted by user.")
                    for event in stream:
                        if stop_checker is not None and stop_checker():
                            raise TurnInterrupted("Interrupted by user.")
                        if getattr(event, "type", None) != "content_block_delta":
                            continue
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", None)
                        if delta_type == "text_delta":
                            text = str(getattr(delta, "text", "") or "")
                            if text and text_callback is not None:
                                streamed_text_delta = True
                                text_callback(text)
                        elif delta_type == "thinking_delta":
                            thinking = str(getattr(delta, "thinking", "") or "")
                            if thinking and thinking_callback is not None:
                                streamed_thinking_delta = True
                                thinking_callback({"event": "delta", "type": "thinking_delta", "delta": thinking})
                        elif delta_type == "redacted_thinking":
                            data = getattr(delta, "data", None)
                            if data and thinking_callback is not None:
                                streamed_redacted_thinking = True
                                thinking_callback({"event": "delta", "type": "redacted_thinking", "delta": str(data)})
                    if stop_checker is not None and stop_checker():
                        raise TurnInterrupted("Interrupted by user.")
                    response = stream.get_final_message()
        except TurnInterrupted:
            raise
        except Exception as exc:
            if (
                text_callback is not None
                and stop_checker is None
                and not streamed_text_delta
                and not streamed_thinking_delta
                and not streamed_redacted_thinking
                and _is_stream_json_parse_error(exc)
            ):
                try:
                    response = self.client.messages.create(**request_kwargs)
                except Exception as fallback_exc:
                    raise _wrap_anthropic_exception(fallback_exc) from fallback_exc
            else:
                raise _wrap_anthropic_exception(exc) from exc
        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        content_blocks: list[dict[str, Any]] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "thinking":
                thinking_block = {
                    "type": "thinking",
                    "thinking": str(getattr(block, "thinking", "") or ""),
                    "signature": str(getattr(block, "signature", "") or ""),
                }
                content_blocks.append(thinking_block)
                if thinking_callback is not None and not streamed_thinking_delta:
                    thinking_callback(dict(thinking_block))
            elif block_type == "redacted_thinking":
                thinking_block = {
                    "type": "redacted_thinking",
                    "data": getattr(block, "data", None),
                }
                content_blocks.append(thinking_block)
                if thinking_callback is not None and not streamed_redacted_thinking:
                    thinking_callback(dict(thinking_block))
            elif block_type == "text":
                text_blocks.append(block.text)
                content_blocks.append({"type": "text", "text": block.text})
            elif block_type == "tool_use":
                tool_input = dict(block.input)
                importance = normalize_tool_importance(tool_input.pop("importance", None))
                tool_call = ToolCall(id=block.id, name=block.name, input=tool_input, importance=importance)
                tool_calls.append(tool_call)
                tool_call_block = {
                    "type": "tool_call",
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "input": tool_call.input,
                }
                if tool_call.importance:
                    tool_call_block["importance"] = tool_call.importance
                content_blocks.append(tool_call_block)
        stop_reason = response.stop_reason or "end_turn"
        if stop_reason == "tool_use":
            stop_reason = "tool_use"
        return AssistantTurn(
            stop_reason=stop_reason,
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            content_blocks=content_blocks,
            usage=self._extract_usage(response),
            raw_response=response,
        )
