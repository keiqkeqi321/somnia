from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from open_somnia.config.models import ProviderSettings
from open_somnia.providers.base import LLMProvider, ProviderError, StopChecker, TextCallback
from open_somnia.runtime.interrupts import TurnInterrupted
from open_somnia.runtime.messages import AssistantTurn, ToolCall

try:
    import tiktoken
except Exception:  # pragma: no cover - optional until dependencies are installed
    tiktoken = None


def _schema_to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        text_parts: list[str] = []
        tool_results: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        for item in content:
            if item["type"] == "text":
                text_parts.append(str(item.get("text", "")))
            elif item["type"] == "tool_result":
                tool_results.append(item)
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
        if text_parts:
            converted.append({"role": role, "content": "\n".join(text_parts)})
        for tool_result in tool_results:
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_result["tool_call_id"],
                    "content": str(tool_result.get("content", "")),
                }
            )
    return converted


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


class OpenAIProvider(LLMProvider):
    def __init__(self, settings: ProviderSettings):
        self.settings = settings

    def count_tokens(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        encoding = _encoding_for_openai_model(self.settings.model)
        payload = {
            "messages": [{"role": "system", "content": system_prompt}] + _to_openai_messages(messages),
            "tools": [_schema_to_openai_tool(tool) for tool in tools],
            "tool_choice": "auto",
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return len(encoding.encode(serialized))

    def token_counter_name(self) -> str:
        return "tiktoken"

    def _extract_usage(self, body: dict[str, Any]) -> dict[str, Any] | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
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
        return {
            "url": f"{self.settings.base_url.rstrip('/')}/chat/completions",
            "body": {
                "model": self.settings.model,
                "messages": [{"role": "system", "content": system_prompt}] + _to_openai_messages(messages),
                "tools": [_schema_to_openai_tool(tool) for tool in tools],
                "tool_choice": "auto",
                "max_tokens": max_tokens,
                "stream": stream,
            },
        }

    def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        text_callback: TextCallback | None = None,
        stop_checker: StopChecker | None = None,
    ) -> AssistantTurn:
        url = f"{self.settings.base_url.rstrip('/')}/chat/completions"
        should_stream = text_callback is not None or stop_checker is not None
        payload = self.debug_request_payload(
            system_prompt,
            messages,
            tools,
            max_tokens,
            stream=should_stream,
        )["body"]
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.organization:
            headers["OpenAI-Organization"] = self.settings.organization
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                if not should_stream:
                    body = json.loads(response.read().decode("utf-8"))
                else:
                    body = self._read_streaming_response(response, text_callback, stop_checker=stop_checker)
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code >= 500 and not _is_overloaded_error(exc.code, details) and not _is_forbidden_like_error(exc.code, details)
            raise ProviderError(
                f"OpenAI request failed: {exc.code} {details}",
                retryable=retryable,
            ) from exc
        except urllib.error.URLError as exc:
            retryable = isinstance(getattr(exc, "reason", None), TimeoutError | socket.timeout) or "timed out" in str(exc).lower()
            raise ProviderError(f"OpenAI request failed: {exc}", retryable=retryable) from exc

        choice = body["choices"][0]
        message = choice["message"]
        text_blocks: list[str] = []
        content = message.get("content")
        if isinstance(content, str) and content:
            text_blocks.append(content)
        elif isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    text_blocks.append(item.get("text", ""))
        tool_calls = [
            ToolCall(
                id=tool_call["id"],
                name=tool_call["function"]["name"],
                input=json.loads(tool_call["function"].get("arguments") or "{}"),
            )
            for tool_call in message.get("tool_calls", [])
        ]
        stop_reason = choice.get("finish_reason") or "stop"
        if stop_reason == "tool_calls":
            stop_reason = "tool_use"
        elif stop_reason == "stop":
            stop_reason = "end_turn"
        return AssistantTurn(
            stop_reason=stop_reason,
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            usage=self._extract_usage(body),
            raw_response=body,
        )

    def _read_streaming_response(
        self,
        response,
        text_callback: TextCallback | None,
        *,
        stop_checker: StopChecker | None = None,
    ) -> dict[str, Any]:
        aggregated_message: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": []}
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"

        for raw_line in response:
            if stop_checker is not None and stop_checker():
                raise TurnInterrupted("Interrupted by user.")
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            choice = event["choices"][0]
            delta = choice.get("delta", {})
            finish_reason = choice.get("finish_reason") or finish_reason

            content = delta.get("content")
            if isinstance(content, str) and content:
                aggregated_message["content"] += content
                if text_callback is not None:
                    text_callback(content)
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            aggregated_message["content"] += text
                            if text_callback is not None:
                                text_callback(text)

            for tool_delta in delta.get("tool_calls", []):
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

        aggregated_message["tool_calls"] = [tool_calls_by_index[index] for index in sorted(tool_calls_by_index)]
        return {
            "choices": [
                {
                    "message": aggregated_message,
                    "finish_reason": finish_reason,
                }
            ]
        }
