from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from open_somnia.runtime.interrupts import TurnInterrupted


@dataclass(slots=True)
class ToolExecutionContext:
    runtime: Any
    session: Any
    actor: str
    trace_id: str
    should_interrupt: Callable[[], bool] | None = None

    def interruption_requested(self) -> bool:
        checker = self.should_interrupt
        return bool(checker()) if callable(checker) else False

    def raise_if_interrupted(self) -> None:
        if self.interruption_requested():
            raise TurnInterrupted("Interrupted by user.")
