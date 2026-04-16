from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class HookPayload:
    raw: dict[str, Any]

    @property
    def event(self) -> str:
        return str(self.raw.get("event", "")).strip()

    @property
    def session_id(self) -> str | None:
        value = self.raw.get("session_id")
        return str(value).strip() if value is not None else None

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


@dataclass(slots=True)
class HookResponse:
    action: str = "continue"
    message: str = ""
    replacement_input: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action}
        if self.message:
            payload["message"] = self.message
        if self.replacement_input is not None:
            payload["replacement_input"] = self.replacement_input
        return payload


def read_payload() -> HookPayload:
    try:
        raw = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid hook payload: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Hook payload must be a JSON object.")
    return HookPayload(raw=raw)


def continue_response(message: str = "") -> HookResponse:
    return HookResponse(action="continue", message=str(message).strip())


def deny_response(message: str) -> HookResponse:
    return HookResponse(action="deny", message=str(message).strip())


def replace_input_response(replacement_input: dict[str, Any], message: str = "") -> HookResponse:
    return HookResponse(
        action="replace_input",
        message=str(message).strip(),
        replacement_input=dict(replacement_input),
    )


def emit_response(response: HookResponse) -> None:
    sys.stdout.write(json.dumps(response.to_payload(), ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


class HookHandler:
    def handle(self, payload: HookPayload) -> HookResponse | None:
        return continue_response()


def run(handler: HookHandler | Callable[[HookPayload], HookResponse | None]) -> int:
    try:
        payload = read_payload()
        if isinstance(handler, HookHandler):
            response = handler.handle(payload)
        else:
            response = handler(payload)
        if response is None:
            response = continue_response()
        emit_response(response)
        return 0
    except Exception as exc:
        print(f"Hook handler failed: {exc}", file=sys.stderr)
        return 1
