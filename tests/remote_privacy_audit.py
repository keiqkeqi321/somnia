from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class PrivacyAuditResult:
    inspected_paths: tuple[str, ...]
    matches: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.matches


def scan_for_sentinel(paths: Iterable[Path], sentinel: str) -> PrivacyAuditResult:
    """Scan declared operational artifacts without copying their contents."""
    needle = str(sentinel)
    if not needle:
        raise ValueError("A non-empty privacy sentinel is required.")
    inspected: list[str] = []
    matches: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        inspected.append(str(path))
        if not path.exists():
            continue
        files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
        for item in files:
            try:
                content = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle in content:
                matches.append(str(item))
    return PrivacyAuditResult(tuple(inspected), tuple(matches))


def write_audit_report(result: PrivacyAuditResult, destination: Path) -> None:
    """Write metadata-only evidence; never include the sentinel itself."""
    payload = {
        "passed": result.passed,
        "inspected_paths": list(result.inspected_paths),
        "match_count": len(result.matches),
        "matched_paths": list(result.matches),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
