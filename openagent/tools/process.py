from __future__ import annotations

import locale
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(slots=True)
class CommandResult:
    args: Any
    returncode: int
    stdout: str
    stderr: str

    def combined_output(self) -> str:
        return f"{self.stdout}{self.stderr}"


def _candidate_encodings() -> list[str]:
    encodings = ["utf-8", "utf-8-sig", locale.getpreferredencoding(False)]
    if os.name == "nt":
        encodings.extend(["mbcs", "gb18030"])

    seen: set[str] = set()
    ordered: list[str] = []
    for encoding in encodings:
        normalized = (encoding or "").strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(encoding)
    return ordered


def decode_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""

    for encoding in _candidate_encodings():
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def run_command(
    command: str | Sequence[str],
    *,
    shell: bool,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    completed = subprocess.run(
        command,
        shell=shell,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=False,
        timeout=timeout,
    )
    return CommandResult(
        args=completed.args,
        returncode=completed.returncode,
        stdout=decode_output(completed.stdout),
        stderr=decode_output(completed.stderr),
    )
