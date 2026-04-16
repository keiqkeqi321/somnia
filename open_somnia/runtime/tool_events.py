from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any

from open_somnia.runtime.execution_mode import AUTHORIZATION_TOOL_NAME, MODE_SWITCH_TOOL_NAME
from open_somnia.runtime.session import AgentSession


class ToolEventRenderer:
    TOOL_VALUE_PREVIEW_CHARS = 90
    TOOL_RESULT_PREVIEW_CHARS = 60
    SILENT_TOOL_NAMES = {"TodoWrite"}

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def print_tool_event(self, actor: str, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
        category = "MCP" if tool_name.startswith("mcp__") else "TOOL"
        log_entry = self.runtime.tool_log_store.write(
            actor=actor,
            tool_name=tool_name,
            tool_input=tool_input,
            output=output,
            category=category,
        )
        if tool_name in self.SILENT_TOOL_NAMES:
            return log_entry["id"]
        if actor != "lead":
            return log_entry["id"]
        if not sys.stdout.isatty():
            return log_entry["id"]
        print()
        for line in self.render_tool_event_lines(tool_name, tool_input, output, log_id=log_entry["id"]):
            print(line)
        print()
        return log_entry["id"]

    def render_tool_event_lines(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        output: Any,
        *,
        log_id: str | None = None,
    ) -> list[str]:
        if self._is_file_change_event(tool_name, output):
            return self._render_file_change_event_lines(tool_name, tool_input, output, log_id=log_id)
        lines = [self._format_tool_heading(tool_name, tool_input, output)]
        lines.extend(self._format_tool_result_lines(tool_name, tool_input, output, log_id))
        return lines

    def _format_tool_heading(self, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
        return f"{self._tool_bullet(output)} {self._tool_title(tool_name, tool_input, output)}"

    def _tool_bullet(self, output: Any) -> str:
        failed = self._tool_event_failed(output)
        bullet = "\u25cf"
        if self.runtime._supports_ansi_output():
            color = "\x1b[31m" if failed else "\x1b[32m"
            bullet = f"{color}{bullet}\x1b[0m"
        return bullet

    def _tool_title(self, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
        if tool_name == "bash":
            command = self.runtime._compact_preview(str(tool_input.get("command", "")).strip(), limit=140)
            return f"Bash({command or '(no command)'})"
        if tool_name == "edit_file":
            path = self._edit_file_primary_path(tool_input, output)
            return f"{self._display_tool_name(tool_name)}({path or '(unknown path)'})"
        if tool_name == "read_file":
            path = str(tool_input.get("path", "")).strip() or "(unknown path)"
            return f"Read({path})"
        if tool_name == "tree":
            path = str(tool_input.get("path", ".")).strip() or "."
            return f"Tree({path})"
        if tool_name == "project_scan":
            path = str(tool_input.get("path", ".")).strip() or "."
            return f"ProjectScan({path})"
        if tool_name == "find_symbol":
            query = str(tool_input.get("query", "")).strip() or "(missing query)"
            return f"FindSymbol({query})"
        if tool_name == AUTHORIZATION_TOOL_NAME:
            target = str(tool_input.get("tool_name", "")).strip() or "tool"
            return f"Authorize({target})"
        if tool_name == MODE_SWITCH_TOOL_NAME:
            target_mode = str(tool_input.get("target_mode", "")).strip() or "mode"
            return f"SwitchMode({target_mode})"
        if tool_name.startswith("mcp__"):
            return self._format_mcp_title(tool_name, tool_input)
        name = self._prettify_tool_name(tool_name)
        args_preview = self._format_tool_args_preview(tool_input)
        if args_preview:
            return f"{name}({args_preview})"
        return name

    def _format_mcp_title(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        parts = tool_name.split("__")
        if len(parts) >= 3:
            server = parts[1]
            tool = parts[2]
            args_preview = self._format_tool_args_preview(tool_input, limit=70)
            if args_preview:
                return f"{server}.{tool}({args_preview})"
            return f"{server}.{tool}"
        return tool_name

    def _prettify_tool_name(self, tool_name: str) -> str:
        display_name = self._display_tool_name(tool_name)
        if display_name != str(tool_name):
            return display_name
        words = [part for part in str(tool_name).replace("__", "_").split("_") if part]
        if not words:
            return tool_name
        return "".join(word.capitalize() for word in words)

    def _display_tool_name(self, tool_name: str) -> str:
        normalized = str(tool_name).strip()
        if normalized == "edit_file":
            return "Update"
        return normalized

    def _format_tool_args_preview(self, tool_input: dict[str, Any], *, limit: int = 96) -> str:
        if not isinstance(tool_input, dict) or not tool_input:
            return ""
        parts: list[str] = []
        for key, value in tool_input.items():
            if key in {"content", "old_text", "new_text"}:
                continue
            rendered = self.runtime._compact_preview(self.runtime._stringify_tool_value(value), limit=40)
            parts.append(f"{key}={rendered}")
        preview = ", ".join(parts)
        return self.runtime._compact_preview(preview, limit=limit)

    def _format_tool_result_lines(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        output: Any,
        log_id: str | None,
    ) -> list[str]:
        summary = self._tool_result_summary(tool_name, tool_input, output)
        lines = self._prefixed_result_block(summary)
        if log_id and self._tool_result_needs_log_hint(tool_name, tool_input, output):
            lines.append(f"     Log: {self._format_tool_log_path(log_id)}")
        return lines

    def _tool_result_summary(self, tool_name: str, tool_input: dict[str, Any], output: Any) -> str:
        limit = self.TOOL_RESULT_PREVIEW_CHARS
        if tool_name == "bash":
            return self.runtime._compact_preview(self.runtime._stringify_tool_value(output), limit=limit) or "(no output)"
        if isinstance(output, dict):
            if "message" in output and str(output.get("message", "")).strip():
                return self.runtime._compact_preview(str(output.get("message", "")).strip(), limit=limit) or "(no output)"
            if "reason" in output and str(output.get("reason", "")).strip():
                return self.runtime._compact_preview(str(output.get("reason", "")).strip(), limit=limit) or "(no output)"
            status = str(output.get("status", "")).strip()
            if status and status not in {"ok", "success", "approved"}:
                return self.runtime._compact_preview(self.runtime._stringify_tool_value(output), limit=limit) or "(no output)"
        return self.runtime._compact_preview(self.runtime._stringify_tool_value(output), limit=limit) or "(no output)"

    def _tool_result_needs_log_hint(self, tool_name: str, tool_input: dict[str, Any], output: Any) -> bool:
        args_text = self.runtime._stringify_tool_value(tool_input)
        result_text = self.runtime._stringify_tool_value(output)
        _, args_hidden = self.runtime._preview_tool_text(args_text, limit=160)
        _, result_hidden = self.runtime._preview_tool_text(result_text, limit=self.TOOL_RESULT_PREVIEW_CHARS)
        return args_hidden or result_hidden

    def _prefixed_result_block(self, text: str) -> list[str]:
        lines = (text or "(no output)").splitlines() or ["(no output)"]
        formatted: list[str] = []
        for index, line in enumerate(lines):
            prefix = "  \u23bf  " if index == 0 else "     "
            formatted.append(prefix + line)
        return formatted

    def _tool_event_failed(self, output: Any) -> bool:
        if isinstance(output, str):
            lowered = output.strip().lower()
            if lowered.startswith("error:") or lowered.startswith("unknown tool:") or lowered.startswith("blocked in "):
                return True
            return False
        if not isinstance(output, dict):
            return False
        status = str(output.get("status", "")).strip().lower()
        if status in {"error", "failed", "denied"}:
            return True
        if status in {"ok", "success", "approved"}:
            return False
        if "success" in output:
            return not bool(output.get("success"))
        error_value = output.get("error")
        return isinstance(error_value, str) and bool(error_value.strip())

    def _is_file_change_event(self, tool_name: str, output: Any) -> bool:
        return tool_name in {"write_file", "edit_file"} and isinstance(output, dict) and "path" in output

    def _render_file_change_event_lines(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        output: dict[str, Any],
        *,
        log_id: str | None = None,
    ) -> list[str]:
        path = str(output.get("path", "(unknown path)"))
        added = int(output.get("added_lines", 0))
        removed = int(output.get("removed_lines", 0))
        lines = [self._format_file_change_heading(tool_name, path, output)]
        summary = self._summarize_file_change(added, removed)
        if summary:
            lines.append(f"  \u23bf  {summary}")
        lines.extend(self._render_file_change_diff(tool_name, tool_input))
        if log_id and self._file_change_needs_log_hint(tool_name, tool_input):
            lines.append(f"     Log: {self._format_tool_log_path(log_id)}")
        return lines

    def _format_file_change_heading(self, tool_name: str, path: str, output: dict[str, Any]) -> str:
        action = "Update"
        if tool_name == "write_file":
            action = "Create" if not bool(output.get("existed_before")) else "Write"
        return f"{self._tool_bullet(output)} {action}({path})"

    def _summarize_file_change(self, added: int, removed: int) -> str:
        if added and removed:
            return f"Added {added} lines, removed {removed} lines"
        if added:
            return f"Added {added} lines"
        if removed:
            return f"Removed {removed} lines"
        return "Updated file"

    def _render_file_change_diff(self, tool_name: str, tool_input: dict[str, Any], *, limit: int = 8) -> list[str]:
        if tool_name == "edit_file":
            return self._render_edit_file_diff(tool_input, limit=limit)
        else:
            before = ""
            after = str(tool_input.get("content", ""))
        if not after and not before:
            return []
        diff_lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1))
        visible = [line for line in diff_lines[2:] if line.strip()]
        if not visible:
            return []
        truncated = False
        if len(visible) > limit:
            visible = visible[:limit]
            truncated = True
        rendered: list[str] = []
        for line in visible:
            rendered.append(self._format_diff_preview_line(line))
        if truncated:
            rendered.append("      ...")
        return rendered

    def _format_diff_preview_line(self, line: str) -> str:
        if self.runtime._supports_ansi_output():
            if line.startswith("+") and not line.startswith("+++"):
                return f"      \x1b[32m{line}\x1b[0m"
            if line.startswith("-") and not line.startswith("---"):
                return f"      \x1b[31m{line}\x1b[0m"
            if line.startswith("@@"):
                return f"      \x1b[38;5;244m{line}\x1b[0m"
        return f"      {line}"

    def _file_change_needs_log_hint(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if tool_name == "edit_file":
            text = self._edit_file_preview_text(tool_input)
        else:
            text = str(tool_input.get("content", ""))
        return len(text.splitlines()) > 8 or len(text) > 240

    def _edit_file_primary_path(self, tool_input: dict[str, Any], output: Any) -> str:
        path = str(tool_input.get("path", "")).strip()
        if path:
            return path
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for item in edits:
                if isinstance(item, dict):
                    item_path = str(item.get("path", "")).strip()
                    if item_path:
                        return item_path
        return str(getattr(output, "get", lambda *_: "")("path", "")).strip()

    def _render_edit_file_diff(self, tool_input: dict[str, Any], *, limit: int = 8) -> list[str]:
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return []
        visible: list[str] = []
        truncated = False
        for index, item in enumerate(edits, start=1):
            if not isinstance(item, dict):
                continue
            before = str(item.get("old_text", ""))
            after = str(item.get("new_text", ""))
            if not before and not after:
                continue
            diff_lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1))
            rendered_lines = [line for line in diff_lines[2:] if line.strip()]
            if not rendered_lines:
                continue
            if len(edits) > 1:
                path_label = str(item.get("path", "")).strip()
                label = f"[edit {index}]"
                if path_label:
                    label = f"{label} {path_label}"
                rendered_lines.insert(0, label)
            remaining = limit - len(visible)
            if remaining <= 0:
                truncated = True
                break
            if len(rendered_lines) > remaining:
                visible.extend(rendered_lines[:remaining])
                truncated = True
                break
            visible.extend(rendered_lines)
        rendered: list[str] = []
        for line in visible:
            if line.startswith("[edit "):
                rendered.append(f"      {line}")
                continue
            rendered.append(self._format_diff_preview_line(line))
        if truncated:
            rendered.append("      ...")
        return rendered

    def _edit_file_preview_text(self, tool_input: dict[str, Any]) -> str:
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return ""
        chunks: list[str] = []
        for item in edits:
            if not isinstance(item, dict):
                continue
            chunks.append(str(item.get("new_text", "")))
        return "\n".join(chunk for chunk in chunks if chunk)

    def _format_clickable_file_label(self, label: str, absolute_path: str) -> str:
        if not absolute_path or not self.runtime._supports_ansi_output():
            return label
        try:
            file_uri = Path(absolute_path).resolve().as_uri()
        except Exception:
            return label
        blue_label = f"\x1b[38;5;39m{label}\x1b[0m"
        return f"\x1b]8;;{file_uri}\x1b\\{blue_label}\x1b]8;;\x1b\\"

    def _format_tool_log_path(self, log_id: str) -> str:
        if not self.runtime._supports_ansi_output():
            return f"/toollog {log_id}"
        root = getattr(self.runtime.tool_log_store, "root", None)
        if not isinstance(root, Path):
            return f"/toollog {log_id}"
        path = root / f"{log_id}.json"
        try:
            relative = path.relative_to(self.runtime.settings.workspace_root)
            label = relative.as_posix()
        except Exception:
            label = str(path)
        return self.runtime._format_clickable_file_label(label, str(path))

    def summarize_file_changes(self, file_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summary_by_path: dict[str, dict[str, Any]] = {}
        for item in file_changes:
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            current = summary_by_path.setdefault(
                path,
                {
                    "path": path,
                    "absolute_path": str(item.get("absolute_path", "")).strip(),
                    "added_lines": 0,
                    "removed_lines": 0,
                },
            )
            current["added_lines"] += int(item.get("added_lines", 0))
            current["removed_lines"] += int(item.get("removed_lines", 0))
            if not current["absolute_path"]:
                current["absolute_path"] = str(item.get("absolute_path", "")).strip()
        return list(summary_by_path.values())

    def print_last_turn_file_summary(self, session: AgentSession) -> bool:
        changes = list(getattr(session, "last_turn_file_changes", []) or [])
        if not changes:
            return False
        print()
        print("Changed files")
        print("Undo by: /undo")
        for item in changes:
            path = str(item.get("path", "(unknown path)"))
            absolute_path = str(item.get("absolute_path", "")).strip()
            plus_text = f"+{int(item.get('added_lines', 0))}"
            minus_text = f"-{int(item.get('removed_lines', 0))}"
            path_text = self.runtime._format_clickable_file_label(path, absolute_path)
            if self.runtime._supports_ansi_output():
                plus_text = f"\x1b[32m{plus_text}\x1b[0m"
                minus_text = f"\x1b[31m{minus_text}\x1b[0m"
            print(f"{path_text} {plus_text} {minus_text}")
        print()
        return True

    def _stdout_is_prompt_toolkit_proxy(self) -> bool:
        stdout_type = type(sys.stdout)
        return stdout_type.__module__ == "prompt_toolkit.patch_stdout" and stdout_type.__name__ == "StdoutProxy"

    def _supports_ansi_output(self) -> bool:
        if self.runtime._ansi_output_enabled is not None:
            return self.runtime._ansi_output_enabled
        if not sys.stdout.isatty():
            self.runtime._ansi_output_enabled = False
            return False
        if sys.platform != "win32":
            self.runtime._ansi_output_enabled = True
            return True
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            if handle in (0, -1):
                self.runtime._ansi_output_enabled = False
                return False
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
                self.runtime._ansi_output_enabled = False
                return False
            enable_vt = 0x0004
            if mode.value & enable_vt:
                self.runtime._ansi_output_enabled = True
                return True
            self.runtime._ansi_output_enabled = kernel32.SetConsoleMode(handle, mode.value | enable_vt) != 0
            return self.runtime._ansi_output_enabled
        except Exception:
            self.runtime._ansi_output_enabled = False
            return False

    def _stringify_tool_value(self, value: Any) -> str:
        if isinstance(value, str):
            return " ".join(value.split())
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return " ".join(str(value).split())

    def _compact_preview(self, text: str, *, limit: int) -> str:
        compact = " ".join(str(text).split())
        if not compact:
            return ""
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    def _preview_tool_text(self, text: str, *, limit: int | None = None) -> tuple[str, bool]:
        preview_limit = self.TOOL_VALUE_PREVIEW_CHARS if limit is None else limit
        compact = self.runtime._compact_preview(text, limit=preview_limit)
        if not compact:
            return "(no output)", False
        return compact, len(" ".join(str(text).split())) > preview_limit

    def recent_tool_logs(self, limit: int = 10) -> str:
        entries = self.runtime.tool_log_store.list_recent(limit=limit)
        if not entries:
            return "No tool logs yet."
        lines: list[str] = []
        for entry in entries:
            lines.append(
                f"- {entry['id']} [{entry['category']}] {entry['actor']} -> {self._display_tool_name(entry['tool_name'])}"
            )
        return "\n".join(lines)

    def render_tool_log(self, log_id: str) -> str:
        entry = self.runtime.tool_log_store.get(log_id)
        if entry is None:
            return f"Tool log '{log_id}' not found."
        args_text = json.dumps(entry["tool_input"], ensure_ascii=False, indent=2)
        return "\n".join(
            [
                f"[tool log {entry['id']}]",
                f"Category: {entry['category']}",
                f"Actor: {entry['actor']}",
                f"Tool: {self._display_tool_name(entry['tool_name'])}",
                "Args:",
                args_text,
                "Result:",
                str(entry["output"]),
            ]
        )
