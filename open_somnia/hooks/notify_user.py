#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


APP_NAME = "Somnia"
TITLE_LIMIT = 64
BODY_LIMIT = 220
WINDOWS_BODY_LIMIT = 180


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"Invalid hook payload: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("Hook payload must be a JSON object.", file=sys.stderr)
        return 1

    title, body = build_notification(payload)
    if not body:
        return 0
    dispatch_notification(title, body)
    return 0


def build_notification(payload: dict[str, object]) -> tuple[str, str]:
    event = str(payload.get("event", "")).strip()
    if event == "AssistantResponse":
        text = stable_preview(str(payload.get("text", "")).strip(), limit=BODY_LIMIT)
        if not text:
            text = "Response completed."
        return stable_preview(APP_NAME, limit=TITLE_LIMIT), text
    if event == "UserChoiceRequested":
        choice_type = str(payload.get("choice_type", "")).strip()
        choice_payload = payload.get("choice_payload", {})
        if not isinstance(choice_payload, dict):
            choice_payload = {}
        if choice_type == "authorization":
            tool_name = stable_preview(str(choice_payload.get("tool_name", "")).strip(), limit=40) or "tool"
            reason = stable_preview(str(choice_payload.get("reason", "")).strip(), limit=120)
            body = f"Authorization required for {tool_name}."
            if reason:
                body += f" {reason}"
            return stable_preview(APP_NAME, limit=TITLE_LIMIT), stable_preview(body, limit=BODY_LIMIT)
        if choice_type == "mode_switch":
            target_mode = stable_preview(str(choice_payload.get("target_mode", "")).strip(), limit=32) or "requested mode"
            reason = stable_preview(str(choice_payload.get("reason", "")).strip(), limit=120)
            body = f"Mode switch required: {target_mode}."
            if reason:
                body += f" {reason}"
            return stable_preview(APP_NAME, limit=TITLE_LIMIT), stable_preview(body, limit=BODY_LIMIT)
        return stable_preview(APP_NAME, limit=TITLE_LIMIT), "Action required."
    if event == "TurnFailed":
        error_type = stable_preview(str(payload.get("error_type", "")).strip(), limit=48)
        error_message = stable_preview(str(payload.get("error_message", "")).strip(), limit=160)
        body = "Turn failed."
        if error_type:
            body += f" {error_type}."
        if error_message:
            body += f" {error_message}"
        return stable_preview(APP_NAME, limit=TITLE_LIMIT), stable_preview(body, limit=BODY_LIMIT)
    return "", ""


def stable_preview(text: str, *, limit: int) -> str:
    collapsed = " ".join(part for part in text.replace("\r", "\n").splitlines() if part.strip())
    cleaned_chars: list[str] = []
    for char in collapsed:
        codepoint = ord(char)
        if codepoint < 32:
            continue
        if codepoint > 0xFFFF:
            cleaned_chars.append("?")
            continue
        cleaned_chars.append(char)
    cleaned = "".join(cleaned_chars).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def dispatch_notification(title: str, body: str) -> None:
    if sys.platform == "win32":
        notify_windows(title, stable_preview(body, limit=WINDOWS_BODY_LIMIT))
        return
    if sys.platform == "darwin":
        notify_macos(title, body)
        return
    notify_linux(title, body)


def notify_windows(title: str, body: str) -> None:
    payload_path = write_temp_payload({"title": title, "body": body})
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    script = (
        "& { "
        "param([string]$payloadPath) "
        "$payload = Get-Content -LiteralPath $payloadPath -Raw -Encoding UTF8 | ConvertFrom-Json; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$notifyIcon = New-Object System.Windows.Forms.NotifyIcon; "
        "$notifyIcon.Icon = [System.Drawing.SystemIcons]::Information; "
        "$notifyIcon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info; "
        "$notifyIcon.BalloonTipTitle = [string]$payload.title; "
        "$notifyIcon.BalloonTipText = [string]$payload.body; "
        "$notifyIcon.Visible = $true; "
        "$notifyIcon.ShowBalloonTip(10000); "
        "Start-Sleep -Seconds 6; "
        "$notifyIcon.Dispose() "
        "}"
    )
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
                str(payload_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    finally:
        payload_path.unlink(missing_ok=True)


def notify_macos(title: str, body: str) -> None:
    script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
    subprocess.run(
        ["osascript", "-e", script, title, body],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def notify_linux(title: str, body: str) -> None:
    if shutil_which("notify-send"):
        subprocess.run(
            ["notify-send", title, body],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )


def write_temp_payload(payload: dict[str, str]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix="somnia-hook-",
        suffix=".json",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    try:
        json.dump(payload, handle, ensure_ascii=False)
    finally:
        handle.close()
    return Path(handle.name)


def shutil_which(name: str) -> str | None:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for raw in paths:
        candidate = Path(raw) / name
        if candidate.exists():
            return str(candidate)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
