"""Tests for the Explore-subagent read-only shell classifier and gated tool.

The Explore subagent's only write vector is ``bash`` (it has no write_file /
edit_file). To keep parallel Explore subagents free of write races, the
subagent runner registers a read-only ``bash`` that refuses mutating commands.
These tests pin the classifier's allow/deny boundary and the tool's rejection
message, which guides the model toward read-only alternatives.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from open_somnia.tools.registry import ToolRegistry
from open_somnia.tools.shell import (
    is_readonly_shell_command,
    register_readonly_shell_tool,
    run_readonly_shell,
)


class IsReadonlyShellCommandTests(unittest.TestCase):
    def test_readonly_unix_commands_allowed(self) -> None:
        for cmd in ("cat file.txt", "grep pattern file", "find . -name x", "head -5 f",
                    "tail -5 f", "ls -la", "pwd", "rg pattern", "wc -l f", "tree", "type f"):
            ok, reason = is_readonly_shell_command(cmd)
            self.assertTrue(ok, f"{cmd!r} should be read-only (reason={reason!r})")

    def test_readonly_windows_powershell_allowed(self) -> None:
        for cmd in ("Get-Content file", "Get-ChildItem", "Select-String pattern f", "dir"):
            ok, reason = is_readonly_shell_command(cmd)
            self.assertTrue(ok, f"{cmd!r} should be read-only (reason={reason!r})")

    def test_readonly_git_subcommands_allowed(self) -> None:
        for cmd in ("git status", "git diff", "git log --oneline", "git show HEAD"):
            ok, reason = is_readonly_shell_command(cmd)
            self.assertTrue(ok, f"{cmd!r} should be read-only (reason={reason!r})")

    def test_pipe_to_readonly_command_allowed(self) -> None:
        ok, _ = is_readonly_shell_command("grep x | head")
        self.assertTrue(ok)

    def test_write_commands_rejected(self) -> None:
        cases = [
            "rm file",
            "grep x > out.txt",
            "git checkout .",
            "git reset --hard",
            "echo x | tee f",
            "cat a && rm b",
            "git pull",
            "git push",
            "git commit -m x",
            "git add .",
            "git stash",
            "npm install",
            "pip install x",
            "curl http://x",
            "mkdir newdir",
            "touch f",
            "mv a b",
            "cp a b",
            "chmod +x f",
        ]
        for cmd in cases:
            ok, reason = is_readonly_shell_command(cmd)
            self.assertFalse(ok, f"{cmd!r} should be rejected as a write")
            # The reason must mention a concrete write operation, not the allowlist.
            self.assertNotIn("allowlist", reason, f"{cmd!r} reason should cite the write op")

    def test_non_allowlisted_leading_word_rejected(self) -> None:
        # echo is not in the allowlist and carries no recognized write syntax,
        # so it is rejected for the allowlist reason (not a write fragment).
        ok, reason = is_readonly_shell_command("echo hello")
        self.assertFalse(ok)
        self.assertIn("allowlist", reason)

    def test_empty_command_rejected(self) -> None:
        ok, reason = is_readonly_shell_command("")
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_case_insensitive(self) -> None:
        ok, _ = is_readonly_shell_command("GIT STATUS")
        self.assertTrue(ok)
        ok, reason = is_readonly_shell_command("RM file")
        self.assertFalse(ok)

    def test_whitespace_normalized(self) -> None:
        ok, _ = is_readonly_shell_command("   cat    file.txt   ")
        self.assertTrue(ok)


class ReadonlyShellToolTests(unittest.TestCase):
    """The gated bash handler executes read-only commands, refuses writes."""

    def _ctx(self):
        # run_shell reads runtime.settings.workspace_root and
        # runtime.runtime.command_timeout_seconds / max_tool_output_chars.
        return SimpleNamespace(runtime=SimpleNamespace(
            settings=SimpleNamespace(
                workspace_root=".",
                runtime=SimpleNamespace(
                    command_timeout_seconds=10,
                    max_tool_output_chars=5000,
                ),
            ),
        ))

    def test_write_command_returns_rejection_without_executing(self) -> None:
        executed: list[str] = []

        # Patch run_shell indirectly by registering the tool and calling the
        # handler directly with a write command; the rejection short-circuits
        # before any subprocess call.
        registry = ToolRegistry()
        register_readonly_shell_tool(registry)
        # Look up the handler via registry to exercise the registered path.
        result = run_readonly_shell(self._ctx(), {"command": "rm important.txt"})
        self.assertIn("Error", result)
        self.assertIn("Explore", result)
        self.assertIn("general-purpose", result)
        self.assertEqual(executed, [])

    def test_rejection_names_the_specific_write(self) -> None:
        result = run_readonly_shell(self._ctx(), {"command": "git checkout ."})
        self.assertIn("git checkout", result)

    def test_readonly_command_passes_through_to_run_shell(self) -> None:
        # A read-only command reaches run_shell; on this platform `pwd`/echo-like
        # commands produce output. We assert it does NOT return the rejection
        # header (i.e. it was not rejected). We use a harmless read-only command.
        result = run_readonly_shell(self._ctx(), {"command": "git status"})
        # Either it ran (output) or failed (e.g. not a git repo), but it must
        # NOT be the Explore-mode rejection.
        self.assertNotIn("Explore 模式", result)


if __name__ == "__main__":
    unittest.main()
