from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from open_somnia.config.models import ProviderSettings
from open_somnia.providers.base import LLMProvider, ProviderError, StopChecker, TextCallback
from open_somnia.reasoning import anthropic_reasoning_payload
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import (
    active_tool_result_content_blocks,
    AssistantTurn,
    ToolCall,
    normalize_tool_importance,
    parse_image_data_url,
    prepare_image_bytes_for_model,
)


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


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        blocks: list[dict[str, Any]] = []
        for item in content:
            if item["type"] == "text":
                blocks.append({"type": "text", "text": str(item.get("text", ""))})
            elif item["type"] in {"image_url", "input_image"}:
                image_block = _anthropic_image_block(item)
                if image_block is not None:
                    blocks.append(image_block)
            elif item["type"] == "thinking":
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": str(item.get("thinking", "")),
                        "signature": str(item.get("signature", "")),
                    }
                )
            elif item["type"] == "redacted_thinking":
                blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": item.get("data"),
                    }
                )
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
        converted.append({"role": role, "content": blocks})
    return converted


def _anthropic_tool_result_content(value: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in active_tool_result_content_blocks(value):
        block_type = str(item.get("type", "")).strip()
        if block_type == "text":
            blocks.append({"type": "text", "text": str(item.get("text", ""))})
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


def _wrap_anthropic_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(
        f"Anthropic request failed: {exc}",
        retryable=_anthropic_exception_retryable(exc),
    )


class AnthropicProvider(LLMProvider):
    def __init__(self, settings: ProviderSettings):
        kwargs: dict[str, Any] = {}
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        if settings.api_key:
            kwargs["api_key"] = settings.api_key
        self.client = Anthropic(**kwargs)
        self.settings = settings

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        response = self.client.messages.count_tokens(
            model=self.settings.model,
            system=system_prompt,
            messages=_to_anthropic_messages(messages),
            tools=tools,
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
        total_tokens = input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "source": "provider",
        }

    def debug_request_payload(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload = {
            "model": self.settings.model,
            "system": system_prompt,
            "messages": _to_anthropic_messages(messages),
            "tools": tools,
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
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        text_callback: TextCallback | None = None,
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
        try:
            if text_callback is None and stop_checker is None:
                response = self.client.messages.create(**request_kwargs)
            else:
                with self.client.messages.stream(**request_kwargs) as stream:
                    if stop_checker is not None and stop_checker():
                        raise TurnInterrupted("Interrupted by user.")
                    for text in stream.text_stream:
                        if stop_checker is not None and stop_checker():
                            raise TurnInterrupted("Interrupted by user.")
                        if text_callback is not None:
                            text_callback(text)
                    if stop_checker is not None and stop_checker():
                        raise TurnInterrupted("Interrupted by user.")
                    response = stream.get_final_message()
        except TurnInterrupted:
            raise
        except Exception as exc:
            raise _wrap_anthropic_exception(exc) from exc
        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        content_blocks: list[dict[str, Any]] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "thinking":
                content_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": str(getattr(block, "thinking", "") or ""),
                        "signature": str(getattr(block, "signature", "") or ""),
                    }
                )
            elif block_type == "redacted_thinking":
                content_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": getattr(block, "data", None),
                    }
                )
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
