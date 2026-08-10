"""Machine-facing CLI output: JSON envelopes, error codes, and exit codes.

This module is the single source of truth for somnia's scriptable contract:
given an input, print a deterministic result (JSON on stdout) or a structured
error (JSON on stderr when --json is active) and exit immediately with a
documented exit code.
"""

from __future__ import annotations

import json
import sys
from typing import Any

EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_QUOTA_EXCEEDED = 2
EXIT_AUTH_FAILED = 3
EXIT_MODEL_ERROR = 4
EXIT_TIMEOUT = 5
EXIT_SESSION_NOT_FOUND = 6
EXIT_CONFIG_ERROR = 7
EXIT_USAGE_ERROR = 64

_ERROR_KIND_EXIT_CODES = {
    "quota": EXIT_QUOTA_EXCEEDED,
    "auth": EXIT_AUTH_FAILED,
    "model": EXIT_MODEL_ERROR,
    "timeout": EXIT_TIMEOUT,
}

_ERROR_KIND_CODES = {
    "quota": "quota_exceeded",
    "auth": "auth_failed",
    "model": "model_error",
    "timeout": "timeout",
}


class CliError(RuntimeError):
    """An error the CLI surfaces as a structured message plus an exit code."""

    def __init__(self, message: str, *, code: str = "internal_error", exit_code: int = EXIT_INTERNAL_ERROR) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def error_code_for_kind(kind: str | None) -> str:
    return _ERROR_KIND_CODES.get(str(kind or ""), "internal_error")


def exit_code_for_error_kind(kind: str | None) -> int:
    return _ERROR_KIND_EXIT_CODES.get(str(kind or ""), EXIT_INTERNAL_ERROR)


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def emit_error_json(code: str, message: str, **extra: Any) -> None:
    envelope: dict[str, Any] = {"error": {"code": code, "message": message}}
    envelope["error"].update(extra)
    print(json.dumps(envelope, ensure_ascii=False, indent=2), file=sys.stderr)
