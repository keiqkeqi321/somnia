from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Callable

from open_somnia.runtime.messages import AssistantTurn, NormalizedMessage


class ProviderError(RuntimeError):
    """Raised when a provider request fails."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


TextCallback = Callable[[str], None]
StopChecker = Callable[[], bool]


class LLMProvider(ABC):
    @abstractmethod
    def complete(
        self,
        system_prompt: str,
        messages: list[NormalizedMessage],
        tools: list[dict[str, Any]],
        max_tokens: int,
        text_callback: TextCallback | None = None,
        stop_checker: StopChecker | None = None,
    ) -> AssistantTurn:
        raise NotImplementedError

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[NormalizedMessage],
        tools: list[dict[str, Any]],
    ) -> int:
        raise NotImplementedError("Token counting is not implemented for this provider.")

    def context_window_tokens(self) -> int | None:
        settings = getattr(self, "settings", None)
        value = getattr(settings, "context_window_tokens", None)
        return int(value) if value is not None else None

    def token_counter_name(self) -> str:
        return "provider"

    def debug_request_payload(
        self,
        system_prompt: str,
        messages: list[NormalizedMessage],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        return {
            "system_prompt": system_prompt,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "stream": stream,
        }


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
