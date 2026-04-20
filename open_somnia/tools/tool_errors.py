from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


TOOL_ERROR_STATUS = "error"
SELF_REPAIRABLE_TOOL_ERROR_TYPES = frozenset({"missing_required_params", "invalid_arguments"})
ERROR_STATUSES = frozenset({"error", "failed", "denied"})


def make_tool_error(
    tool_name: str,
    error_type: str,
    message: str,
    *,
    argument_path: str | None = None,
    missing_params: list[str] | None = None,
    repair_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": TOOL_ERROR_STATUS,
        "error_type": str(error_type).strip() or "tool_execution_failed",
        "tool_name": str(tool_name).strip() or "tool",
        "message": str(message).strip() or "Tool execution failed.",
    }
    if argument_path:
        payload["argument_path"] = str(argument_path).strip()
    if missing_params:
        normalized = [str(item).strip() for item in missing_params if str(item).strip()]
        if normalized:
            payload["missing_params"] = normalized
    if repair_hint:
        payload["repair_hint"] = repair_hint
    return payload


def validate_tool_payload(tool_name: str, payload: Any, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(schema, dict) or not schema:
        return None
    return _validate_against_schema(tool_name, payload, schema, path="")


def normalize_tool_output(tool_name: str, output: Any, schema: dict[str, Any] | None = None) -> Any:
    if isinstance(output, dict):
        status = str(output.get("status", "")).strip().lower()
        if status in ERROR_STATUSES:
            normalized = dict(output)
            normalized["status"] = TOOL_ERROR_STATUS
            normalized.setdefault("tool_name", str(tool_name).strip() or "tool")
            if not str(normalized.get("error_type", "")).strip():
                normalized["error_type"] = _classify_message_error_type(str(normalized.get("message", "")))
            return normalized
        return output
    if not isinstance(output, str):
        return output
    message = output.strip()
    if not message:
        return output
    lowered = message.lower()
    if lowered.startswith("unknown tool:"):
        return make_tool_error(
            tool_name,
            "unknown_tool",
            message,
        )
    if lowered.startswith("blocked in "):
        return make_tool_error(
            tool_name,
            "blocked_by_execution_mode",
            message,
        )
    if lowered.startswith("error:"):
        body = message.split(":", 1)[1].strip() or message
        return make_tool_error(
            tool_name,
            _classify_message_error_type(body),
            body,
            repair_hint=_repair_hint_for_message(schema, body),
        )
    return output


def tool_error_from_exception(tool_name: str, exc: Exception, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(exc, KeyError):
        missing = _normalize_key_name(exc.args[0] if exc.args else "")
        missing_params = [missing] if missing else None
        return make_tool_error(
            tool_name,
            "missing_required_params",
            _missing_required_message(tool_name, missing_params or []),
            missing_params=missing_params,
            repair_hint=_required_signature(schema),
        )

    message = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, FileNotFoundError):
        return make_tool_error(tool_name, "file_not_found", message)
    if isinstance(exc, PermissionError):
        return make_tool_error(tool_name, "permission_denied", message)
    if isinstance(exc, ValueError):
        return make_tool_error(
            tool_name,
            _classify_message_error_type(message),
            message,
            repair_hint=_repair_hint_for_message(schema, message),
        )
    if isinstance(exc, TypeError):
        return make_tool_error(
            tool_name,
            "invalid_arguments",
            message,
            repair_hint=_schema_signature(schema),
        )
    if isinstance(exc, OSError):
        return make_tool_error(tool_name, "io_error", message)
    return make_tool_error(tool_name, "tool_execution_failed", message)


def sanitize_tool_output_for_persistence(output: Any) -> Any:
    if not isinstance(output, dict):
        return output
    try:
        sanitized = deepcopy(output)
    except Exception:
        sanitized = dict(output)
    sanitized.pop("repair_hint", None)
    return sanitized


def serialize_tool_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return str(output)


def extract_transient_repair_hint(output: Any) -> dict[str, Any] | None:
    if not isinstance(output, dict):
        return None
    if str(output.get("status", "")).strip().lower() not in ERROR_STATUSES:
        return None
    error_type = str(output.get("error_type", "")).strip().lower()
    repair_hint = output.get("repair_hint")
    if error_type not in SELF_REPAIRABLE_TOOL_ERROR_TYPES or not isinstance(repair_hint, dict) or not repair_hint:
        return None
    payload = {
        "tool_name": str(output.get("tool_name", "tool")).strip() or "tool",
        "error_type": error_type,
        "message": str(output.get("message", "")).strip(),
        "repair_hint": repair_hint,
    }
    argument_path = str(output.get("argument_path", "")).strip()
    if argument_path:
        payload["argument_path"] = argument_path
    missing_params = output.get("missing_params")
    if isinstance(missing_params, list) and missing_params:
        payload["missing_params"] = [str(item).strip() for item in missing_params if str(item).strip()]
    return payload


def render_transient_repair_hint_message(hints: list[dict[str, Any]]) -> str:
    compact_hints = [hint for hint in hints if isinstance(hint, dict) and hint]
    if not compact_hints:
        return ""
    return (
        "<tool-repair-hints>"
        "Retry the failed tool call using only these minimal argument hints.\n"
        f"{json.dumps(compact_hints, ensure_ascii=False, separators=(',', ':'), default=str)}\n"
        "</tool-repair-hints>"
    )


def _validate_against_schema(tool_name: str, value: Any, schema: dict[str, Any], *, path: str) -> dict[str, Any] | None:
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        message = (
            f"Invalid arguments for '{tool_name}': {_display_path(path)} "
            f"must be {_expected_type_label(schema)} (got {_value_type_label(value)})."
        )
        return make_tool_error(
            tool_name,
            "invalid_arguments",
            message,
            argument_path=path or None,
            repair_hint=_type_repair_hint(schema, path),
        )

    if "enum" in schema and value not in list(schema.get("enum") or []):
        allowed = ", ".join(json.dumps(item, ensure_ascii=False) for item in list(schema.get("enum") or []))
        return make_tool_error(
            tool_name,
            "invalid_arguments",
            f"Invalid arguments for '{tool_name}': {_display_path(path)} must be one of [{allowed}].",
            argument_path=path or None,
            repair_hint=_type_repair_hint(schema, path),
        )

    if "const" in schema and value != schema.get("const"):
        return make_tool_error(
            tool_name,
            "invalid_arguments",
            f"Invalid arguments for '{tool_name}': {_display_path(path)} must equal {json.dumps(schema.get('const'), ensure_ascii=False)}.",
            argument_path=path or None,
            repair_hint=_type_repair_hint(schema, path),
        )

    if isinstance(value, dict):
        required = [str(item).strip() for item in list(schema.get("required") or []) if str(item).strip()]
        missing = [item for item in required if item not in value]
        if missing:
            return make_tool_error(
                tool_name,
                "missing_required_params",
                _missing_required_message(tool_name, missing, path=path),
                argument_path=path or None,
                missing_params=missing,
                repair_hint=_required_signature(schema),
            )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if key not in value or not isinstance(subschema, dict):
                    continue
                error = _validate_against_schema(tool_name, value[key], subschema, path=_join_path(path, str(key)))
                if error is not None:
                    return error

    if isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                error = _validate_against_schema(tool_name, item, items_schema, path=_join_index(path, index))
                if error is not None:
                    return error

    for subschema in list(schema.get("allOf") or []):
        if not isinstance(subschema, dict):
            continue
        error = _validate_against_schema(tool_name, value, subschema, path=path)
        if error is not None:
            return error

    conditional = schema.get("if")
    if isinstance(conditional, dict):
        if _validate_against_schema(tool_name, value, conditional, path=path) is None:
            then_schema = schema.get("then")
            if isinstance(then_schema, dict):
                error = _validate_against_schema(tool_name, value, then_schema, path=path)
                if error is not None:
                    return error
        else:
            else_schema = schema.get("else")
            if isinstance(else_schema, dict):
                error = _validate_against_schema(tool_name, value, else_schema, path=path)
                if error is not None:
                    return error

    return None


def _matches_type(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_type(value, item) for item in expected_type)
    normalized = str(expected_type).strip().lower()
    if normalized == "object":
        return isinstance(value, dict)
    if normalized == "array":
        return isinstance(value, list)
    if normalized == "string":
        return isinstance(value, str)
    if normalized == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if normalized == "boolean":
        return isinstance(value, bool)
    if normalized == "null":
        return value is None
    return True


def _value_type_label(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _expected_type_label(schema: dict[str, Any]) -> str:
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        rendered = ", ".join(json.dumps(item, ensure_ascii=False) for item in enum)
        return f"one of [{rendered}]"
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        return " or ".join(str(item).strip() for item in expected_type if str(item).strip())
    if expected_type is not None:
        return str(expected_type).strip() or "a valid value"
    if "const" in schema:
        return json.dumps(schema.get("const"), ensure_ascii=False)
    if isinstance(schema.get("properties"), dict) or isinstance(schema.get("required"), list):
        return "object"
    if isinstance(schema.get("items"), dict):
        return "array"
    return "a valid value"


def _schema_signature(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    signature: dict[str, Any] = {}
    required = [str(item).strip() for item in list(schema.get("required") or []) if str(item).strip()]
    if required:
        signature["required"] = required
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        rendered: dict[str, str] = {}
        for key, subschema in properties.items():
            if not isinstance(subschema, dict):
                continue
            rendered[str(key)] = _expected_type_label(subschema)
        if rendered:
            signature["properties"] = rendered
    return signature or None


def _required_signature(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    required = [str(item).strip() for item in list(schema.get("required") or []) if str(item).strip()]
    if not required:
        return None
    return {"required": required}


def _type_repair_hint(schema: dict[str, Any] | None, path: str) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    hint = _required_signature(schema) or {}
    normalized_path = str(path).strip()
    if normalized_path and "." not in normalized_path and "[" not in normalized_path:
        hint["properties"] = {normalized_path: _expected_type_label(schema)}
    elif not normalized_path:
        signature = _schema_signature(schema)
        if signature:
            hint.update(signature)
    return hint or None


def _repair_hint_for_message(schema: dict[str, Any] | None, message: str) -> dict[str, Any] | None:
    lowered = str(message).strip().lower()
    if "required" in lowered or "must contain" in lowered:
        return _required_signature(schema)
    if any(token in lowered for token in ("must be", "non-empty", "invalid", "expected", "one of")):
        return _schema_signature(schema)
    return None


def _classify_message_error_type(message: str) -> str:
    lowered = str(message).strip().lower()
    if not lowered:
        return "tool_execution_failed"
    if "path escapes workspace" in lowered:
        return "path_outside_workspace"
    if "permission denied" in lowered or "access denied" in lowered:
        return "permission_denied"
    if "not found" in lowered or "no such file" in lowered:
        return "file_not_found"
    if any(token in lowered for token in ("required", "must contain", "must be", "non-empty", "invalid", "one of")):
        return "invalid_arguments"
    return "tool_execution_failed"


def _display_path(path: str) -> str:
    normalized = str(path).strip()
    return normalized if normalized else "payload"


def _join_path(base: str, part: str) -> str:
    normalized_base = str(base).strip()
    normalized_part = str(part).strip()
    if not normalized_base:
        return normalized_part
    if not normalized_part:
        return normalized_base
    return f"{normalized_base}.{normalized_part}"


def _join_index(base: str, index: int) -> str:
    normalized_base = str(base).strip()
    if not normalized_base:
        return f"[{index}]"
    return f"{normalized_base}[{index}]"


def _missing_required_message(tool_name: str, missing_params: list[str], *, path: str = "") -> str:
    location = f" at {path}" if str(path).strip() else ""
    joined = ", ".join(missing_params) if missing_params else "(unknown)"
    return f"Missing required parameter(s) for '{tool_name}'{location}: {joined}."


def _normalize_key_name(value: Any) -> str:
    text = str(value).strip()
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    return text
