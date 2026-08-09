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
    # The id of the tool_call this context was built for. Populated by
    # ``execute_tool_call`` (which has the tool_call in hand) so tools that
    # need a per-call-stable identifier -- e.g. the subagent handler, whose
    # checkpoint key MUST match the lead's resume_from pointer (also
    # tool_call.id) -- can read it without the handler signature changing.
    # Default ``None``: callers that don't care (most tools, fake runtimes in
    # tests) leave it unset.
    tool_call_id: str | None = None

    def interruption_requested(self) -> bool:
        checker = self.should_interrupt
        return bool(checker()) if callable(checker) else False

    def raise_if_interrupted(self) -> None:
        if self.interruption_requested():
            raise TurnInterrupted("Interrupted by user.")
