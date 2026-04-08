from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolExecutionContext:
    runtime: Any
    session: Any
    actor: str
    trace_id: str
