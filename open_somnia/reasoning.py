from __future__ import annotations

from typing import Any


REASONING_LEVEL_VALUES = ("low", "medium", "high", "deep")
OPENAI_EFFORT_BY_REASONING_LEVEL = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "deep": "xhigh",
}
ANTHROPIC_ADAPTIVE_EFFORT_BY_REASONING_LEVEL = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "deep": "max",
}
ANTHROPIC_BUDGET_BY_REASONING_LEVEL = {
    "low": 2_048,
    "medium": 8_192,
    "high": 16_384,
    "deep": 32_768,
}
ANTHROPIC_ADAPTIVE_MODEL_HINTS = (
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-mythos",
)
OPENAI_REASONING_MODEL_HINTS = (
    "gpt-5",
    "gpt-oss",
    "o1",
    "o3",
    "o4",
    "codex",
)


def normalize_reasoning_level(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in REASONING_LEVEL_VALUES:
        return normalized
    return None


def _normalized_model_name(model: str) -> str:
    normalized = str(model or "").strip().lower()
    normalized = normalized.replace("_", "-")
    normalized = normalized.replace(".", "-")
    return normalized


def supports_openai_reasoning_model(model: str, *, override: bool | None = None) -> bool:
    if override is not None:
        return bool(override)
    # Keep the default permissive: an unset reasoning_level already disables
    # payload injection, so an unset supports_reasoning flag should behave like on.
    return True


def supports_anthropic_reasoning_model(model: str, *, override: bool | None = None) -> bool:
    if override is not None:
        return bool(override)
    # Keep the default permissive: an unset reasoning_level already disables
    # payload injection, so an unset supports_reasoning flag should behave like on.
    return True


def supports_anthropic_adaptive_model(model: str, *, override: bool | None = None) -> bool:
    if override is not None:
        return bool(override)
    normalized = _normalized_model_name(model)
    return any(hint in normalized for hint in ANTHROPIC_ADAPTIVE_MODEL_HINTS)


def openai_reasoning_payload(
    *,
    model: str,
    reasoning_level: str | None,
    supports_reasoning: bool | None = None,
) -> dict[str, Any]:
    normalized_level = normalize_reasoning_level(reasoning_level)
    if normalized_level is None:
        return {}
    if not supports_openai_reasoning_model(model, override=supports_reasoning):
        return {}
    effort = OPENAI_EFFORT_BY_REASONING_LEVEL[normalized_level]
    normalized_model = _normalized_model_name(model)
    if normalized_model.endswith("-pro") and effort == "low":
        effort = "medium"
    return {"reasoning": {"effort": effort}}


def anthropic_reasoning_payload(
    *,
    model: str,
    reasoning_level: str | None,
    max_tokens: int,
    supports_reasoning: bool | None = None,
    supports_adaptive_reasoning: bool | None = None,
) -> dict[str, Any]:
    normalized_level = normalize_reasoning_level(reasoning_level)
    if normalized_level is None:
        return {}
    if not supports_anthropic_reasoning_model(model, override=supports_reasoning):
        return {}
    if supports_anthropic_adaptive_model(model, override=supports_adaptive_reasoning):
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": ANTHROPIC_ADAPTIVE_EFFORT_BY_REASONING_LEVEL[normalized_level]},
        }
    if int(max_tokens or 0) <= 1_024:
        return {}
    budget_tokens = min(ANTHROPIC_BUDGET_BY_REASONING_LEVEL[normalized_level], int(max_tokens) - 1)
    if budget_tokens < 1_024:
        return {}
    return {
        "thinking": {
            "type": "enabled",
            "budget_tokens": budget_tokens,
        }
    }
