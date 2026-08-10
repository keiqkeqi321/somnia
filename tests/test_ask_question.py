from __future__ import annotations

import threading
import time
import unittest

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from open_somnia.cli.prompting import _build_question_application


DOWN = "\x1b[B"
UP = "\x1b[A"
ENTER = "\r"
ESCAPE = "\x1b"


def _drive_question_app(
    steps: list,
    *,
    options: tuple[str, ...] = ("Option A", "Option B"),
    allow_custom: bool = True,
    settle_seconds: float = 0.8,
):
    """Run the inline question widget headless and feed it keystrokes.

    Steps are key strings to send; a callable step is invoked with the app
    instead, which lets tests observe widget state between keystrokes.
    """
    with create_pipe_input() as inp:
        app = _build_question_application(
            "Which approach?",
            list(options),
            allow_custom,
            input=inp,
            output=DummyOutput(),
        )

        def feed() -> None:
            time.sleep(0.3)
            for step in steps:
                if callable(step):
                    step(app)
                else:
                    inp.send_text(step)
                time.sleep(0.15)
            time.sleep(settle_seconds)
            try:
                future = getattr(app, "future", None)
                if future is not None and not future.done():
                    app.exit(result=("__timeout__", ""))
            except Exception:
                pass

        threading.Thread(target=feed, daemon=True).start()
        return app.run()


class AskQuestionInlineTests(unittest.TestCase):
    def test_down_wraps_into_custom_input_and_cycles_back_to_first_option(self) -> None:
        # A -> B -> custom input -> B -> custom input -> wrap to A -> confirm.
        result = _drive_question_app([DOWN, DOWN, UP, DOWN, DOWN, ENTER])

        self.assertEqual(result, ("option", "Option A"))

    def test_up_at_first_option_focuses_custom_input(self) -> None:
        result = _drive_question_app([UP, "hello", ENTER])

        self.assertEqual(result, ("custom", "hello"))

    def test_custom_answer_is_submitted_from_the_input_row(self) -> None:
        result = _drive_question_app([DOWN, DOWN, "my own answer", ENTER])

        self.assertEqual(result, ("custom", "my own answer"))

    def test_escape_does_not_cancel(self) -> None:
        # Esc is not a hidden cancel: the question must be answered.
        result = _drive_question_app([ESCAPE, ENTER], settle_seconds=1.5)

        self.assertEqual(result, ("option", "Option A"))

    def test_navigation_stops_at_last_option_when_custom_disabled(self) -> None:
        result = _drive_question_app([DOWN, DOWN, ENTER], allow_custom=False)

        self.assertEqual(result, ("option", "Option B"))

    def test_options_uncheck_while_custom_input_has_focus(self) -> None:
        observed: dict[str, object] = {}

        def note_in_input(app) -> None:
            observed["in_input"] = app._question_radio_list.current_value

        def note_back_on_list(app) -> None:
            observed["back"] = app._question_radio_list.current_value

        result = _drive_question_app([DOWN, DOWN, note_in_input, UP, note_back_on_list, ENTER])

        self.assertIsNone(observed["in_input"])
        self.assertEqual(observed["back"], "Option B")
        self.assertEqual(result, ("option", "Option B"))


if __name__ == "__main__":
    unittest.main()
