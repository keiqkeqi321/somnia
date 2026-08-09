from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from open_somnia.cli.help import (
    ALL_COMMANDS,
    REPL_COMMAND_SPECS,
    cli_help,
    detail_json,
    lookup,
    overview_json,
    render_detail,
    render_overview,
    render_repl_help,
    unknown_json,
)
from open_somnia.cli.main import build_parser, main
from open_somnia.cli.prompting import COMMAND_SPECS


class HelpLookupTests(unittest.TestCase):
    def test_lookup_cli_command(self) -> None:
        self.assertEqual(lookup("run").name, "run")

    def test_lookup_repl_command_with_and_without_slash(self) -> None:
        self.assertEqual(lookup("/rollback").name, "/rollback")
        self.assertEqual(lookup("rollback").name, "/rollback")

    def test_lookup_global_option(self) -> None:
        self.assertEqual(lookup("--provider").name, "--provider")
        self.assertEqual(lookup("-r").name, "-r")

    def test_lookup_strips_somnia_prefix(self) -> None:
        self.assertEqual(lookup("somnia run").name, "run")

    def test_lookup_case_insensitive(self) -> None:
        self.assertEqual(lookup("RUN").name, "run")

    def test_lookup_unknown_returns_none(self) -> None:
        self.assertIsNone(lookup("no-such-command"))

    def test_registry_has_no_duplicate_names(self) -> None:
        names = [spec.name for spec in ALL_COMMANDS]
        self.assertEqual(len(names), len(set(names)))


class HelpRenderingTests(unittest.TestCase):
    def test_overview_mentions_intro_and_all_sections(self) -> None:
        text = render_overview()
        self.assertIn("Somnia", text)
        self.assertIn("CLI commands:", text)
        self.assertIn("CLI options:", text)
        self.assertIn("REPL commands", text)
        for spec in ALL_COMMANDS:
            self.assertIn(spec.name, text)

    def test_detail_includes_usage_options_examples(self) -> None:
        text = render_detail(lookup("run"))
        self.assertIn("somnia run <prompt>", text)
        self.assertIn("--provider", text)
        self.assertIn("Examples:", text)

    def test_detail_repl_has_examples_but_no_usage(self) -> None:
        text = render_detail(lookup("/rollback"))
        self.assertNotIn("Usage:", text)
        self.assertIn("Examples:", text)

    def test_repl_help_lists_all_repl_commands(self) -> None:
        text = render_repl_help()
        for spec in REPL_COMMAND_SPECS:
            self.assertIn(spec.name, text)

    def test_repl_help_topic_renders_detail(self) -> None:
        text = render_repl_help("rollback")
        self.assertIn("/rollback", text)

    def test_repl_help_unknown_topic(self) -> None:
        self.assertIn("[unknown command]", render_repl_help("nope"))


class HelpJsonTests(unittest.TestCase):
    def test_overview_json_shape(self) -> None:
        payload = overview_json()
        self.assertEqual(payload["somnia"]["name"], "somnia")
        self.assertGreaterEqual(len(payload["commands"]), 40)
        sections = {cmd["section"] for cmd in payload["commands"]}
        self.assertEqual(sections, {"cli", "option", "repl"})
        for cmd in payload["commands"]:
            self.assertIn("name", cmd)
            self.assertIn("description", cmd)
            self.assertIn("usage", cmd)

    def test_detail_json_full_fields(self) -> None:
        payload = detail_json(lookup("run"))
        topic = payload["topic"]
        self.assertIn("detail", topic)
        self.assertIn("options", topic)
        self.assertIn("examples", topic)

    def test_unknown_json_has_error_and_commands(self) -> None:
        payload = unknown_json("bogus")
        self.assertIn("error", payload)
        self.assertGreaterEqual(len(payload["commands"]), 1)

    def test_json_output_is_parseable(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = cli_help("run", as_json=True)
        self.assertEqual(status, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["topic"]["name"], "run")


class HelpCliTests(unittest.TestCase):
    def test_cli_help_overview_returns_zero(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = cli_help(None)
        self.assertEqual(status, 0)
        self.assertIn("CLI commands:", stream.getvalue())

    def test_cli_help_unknown_returns_two(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            status = cli_help("no-such")
        self.assertEqual(status, 2)

    def test_parser_accepts_single_dash_help_with_topic(self) -> None:
        args = build_parser().parse_args(["-help", "run"])
        self.assertEqual(args.help_cmd, "run")

    def test_parser_accepts_help_subcommand_with_topic(self) -> None:
        args = build_parser().parse_args(["help", "tasks"])
        self.assertEqual(args.command, "help")
        self.assertEqual(args.topic, "tasks")

    def test_main_dash_help_dispatches(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main(["-help", "run"])
        self.assertEqual(status, 0)
        self.assertIn("somnia run <prompt>", stream.getvalue())

    def test_main_help_subcommand_dispatches(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main(["help"])
        self.assertEqual(status, 0)
        self.assertIn("CLI commands:", stream.getvalue())

    def test_main_dash_help_json(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main(["-help", "--json"])
        self.assertEqual(status, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["somnia"]["name"], "somnia")

    def test_main_help_does_not_require_provider(self) -> None:
        # No provider config needed: help is resolved before load_settings.
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main(["-help"])
        self.assertEqual(status, 0)
        self.assertIn("somnia", stream.getvalue())


class PromptingDerivationTests(unittest.TestCase):
    def test_command_specs_derived_from_registry(self) -> None:
        registry_names = [spec.name for spec in REPL_COMMAND_SPECS]
        derived_names = [name for name, _ in COMMAND_SPECS]
        self.assertEqual(derived_names, registry_names)

    def test_visible_and_hidden_cover_all(self) -> None:
        from open_somnia.cli.prompting import HIDDEN_COMMAND_SPECS, VISIBLE_COMMAND_SPECS

        all_names = [name for name, _ in (VISIBLE_COMMAND_SPECS + HIDDEN_COMMAND_SPECS)]
        self.assertEqual(sorted(all_names), sorted(spec.name for spec in REPL_COMMAND_SPECS))


def redirect_stderr(stream):
    import sys
    from contextlib import redirect_stderr as _rs

    return _rs(stream)


if __name__ == "__main__":
    unittest.main()
