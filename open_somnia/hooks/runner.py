from __future__ import annotations

import json
import os
import subprocess
from threading import Thread
import time
from pathlib import Path
from typing import Callable

from open_somnia.config.models import HookSettings
from open_somnia.hooks.models import HookContext, HookDecision, HookExecutionError, HookExecutionResult


class HookRunner:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def run(self, hook: HookSettings, context: HookContext) -> HookExecutionResult:
        started = time.time()
        command, payload, env, cwd = self._build_invocation(hook, context)
        try:
            completed = subprocess.run(
                command,
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd),
                env=env,
                timeout=max(1, int(hook.timeout_seconds)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HookExecutionError(
                f"Hook '{hook.event}' timed out after {hook.timeout_seconds}s: {hook.command}"
            ) from exc
        except OSError as exc:
            raise HookExecutionError(
                f"Hook '{hook.event}' failed to start '{hook.command}': {exc}"
            ) from exc
        duration_ms = max(0, int((time.time() - started) * 1000))
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            details = stderr or stdout or f"exit code {completed.returncode}"
            raise HookExecutionError(
                f"Hook '{hook.event}' command '{hook.command}' failed: {details}"
            )
        response_payload: dict[str, object] = {}
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise HookExecutionError(
                    f"Hook '{hook.event}' returned invalid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise HookExecutionError(
                    f"Hook '{hook.event}' must return a JSON object when stdout is not empty."
                )
            response_payload = parsed
        decision = self._parse_decision(hook, response_payload)
        return HookExecutionResult(
            hook=hook,
            decision=decision,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            response_payload=response_payload,
        )

    def run_background(
        self,
        hook: HookSettings,
        context: HookContext,
        *,
        on_complete: Callable[[HookExecutionResult | None, HookExecutionError | None], None] | None = None,
    ) -> HookExecutionResult:
        started = time.time()
        command, payload, env, cwd = self._build_invocation(hook, context)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd),
                env=env,
            )
        except OSError as exc:
            raise HookExecutionError(
                f"Hook '{hook.event}' failed to start '{hook.command}': {exc}"
            ) from exc

        def _watch() -> None:
            watch_started = time.time()
            try:
                assert process.stdin is not None
                stdout, stderr = process.communicate(
                    input=payload,
                    timeout=max(1, int(hook.timeout_seconds)),
                )
                stdout = (stdout or "").strip()
                stderr = (stderr or "").strip()
                if process.returncode != 0:
                    details = stderr or stdout or f"exit code {process.returncode}"
                    raise HookExecutionError(
                        f"Hook '{hook.event}' command '{hook.command}' failed: {details}"
                    )
                result = HookExecutionResult(
                    hook=hook,
                    decision=HookDecision(action="continue"),
                    duration_ms=max(0, int((time.time() - watch_started) * 1000)),
                    status="ok",
                    background=True,
                    pid=process.pid,
                    stdout=stdout,
                    stderr=stderr,
                )
                if on_complete is not None:
                    on_complete(result, None)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                if on_complete is not None:
                    on_complete(
                        None,
                        HookExecutionError(
                            f"Hook '{hook.event}' timed out after {hook.timeout_seconds}s: {hook.command}"
                        ),
                    )
            except HookExecutionError as exc:
                if on_complete is not None:
                    on_complete(None, exc)

        Thread(target=_watch, name=f"somnia-hook-{hook.event}", daemon=True).start()
        return HookExecutionResult(
            hook=hook,
            decision=HookDecision(action="continue"),
            duration_ms=max(0, int((time.time() - started) * 1000)),
            status="queued",
            background=True,
            pid=process.pid,
        )

    def _build_invocation(self, hook: HookSettings, context: HookContext) -> tuple[list[str], str, dict[str, str], Path]:
        command = [self._resolve_command(hook.command), *hook.args]
        payload = json.dumps(context.to_payload(), ensure_ascii=False)
        env = os.environ.copy()
        env.update(hook.env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        cwd = hook.cwd or self.workspace_root
        return command, payload, env, cwd

    def _resolve_command(self, command: str) -> str:
        raw = str(command).strip()
        if not raw:
            return raw
        candidate = Path(raw)
        if candidate.is_absolute():
            return str(candidate)
        if any(token in raw for token in ("/", "\\")):
            return str((self.workspace_root / candidate).resolve())
        return raw

    def _parse_decision(self, hook: HookSettings, payload: dict[str, object]) -> HookDecision:
        action = str(payload.get("action", "continue")).strip().lower() or "continue"
        if action == "continue":
            return HookDecision(action="continue", message=str(payload.get("message", "")).strip())
        if hook.event != "PreToolUse":
            raise HookExecutionError(
                f"Hook '{hook.event}' cannot return action '{action}'. Only PreToolUse can alter execution."
            )
        if action == "deny":
            return HookDecision(action="deny", message=str(payload.get("message", "")).strip())
        if action == "replace_input":
            replacement = payload.get("replacement_input", payload.get("tool_input"))
            if not isinstance(replacement, dict):
                raise HookExecutionError(
                    "PreToolUse hooks returning 'replace_input' must include a JSON object in 'replacement_input'."
                )
            return HookDecision(
                action="replace_input",
                message=str(payload.get("message", "")).strip(),
                replacement_input=replacement,
            )
        raise HookExecutionError(f"Unsupported hook action '{action}'.")
