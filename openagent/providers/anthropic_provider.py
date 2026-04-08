from __future__ import annotations

from typing import Any

from anthropic import Anthropic

from openagent.config.models import ProviderSettings
from openagent.providers.base import LLMProvider, StopChecker, TextCallback
from openagent.runtime.interrupts import TurnInterrupted
from openagent.runtime.messages import AssistantTurn, ToolCall


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
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": item["tool_call_id"],
                        "content": str(item.get("content", "")),
                        "is_error": bool(item.get("is_error", False)),
                    }
                )
        converted.append({"role": role, "content": blocks})
    return converted


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

    def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        text_callback: TextCallback | None = None,
        stop_checker: StopChecker | None = None,
    ) -> AssistantTurn:
        request_kwargs = {
            "model": self.settings.model,
            "system": system_prompt,
            "messages": _to_anthropic_messages(messages),
            "tools": tools,
            "max_tokens": max_tokens,
        }
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
        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_blocks.append(block.text)
            elif getattr(block, "type", None) == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        stop_reason = response.stop_reason or "end_turn"
        if stop_reason == "tool_use":
            stop_reason = "tool_use"
        return AssistantTurn(
            stop_reason=stop_reason,
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            raw_response=response,
        )
