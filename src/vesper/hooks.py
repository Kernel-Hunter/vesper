"""Hooks: your own commands that run around the agent's steps.

Configured in `~/.vesper/hooks.json` (yours, for every project) and in a project's
`.vesper/hooks.json`, in the same shape as Claude Code's hooks, so existing ones can be
copied over:

    {"hooks": {"PostToolUse": [{"matcher": "write_file|edit_file",
                                "hooks": [{"type": "command", "command": "ruff format ."}]}]}}

Events:
- PreToolUse: before a tool runs. Exit code 2 blocks the step; the hook's stderr goes back to
  the agent as the reason.
- PostToolUse: after a tool succeeded. Exit code 2 adds the hook's stderr to the result.
- UserPromptSubmit: before a message goes to the model. Exit code 2 blocks the message (the
  user sees stderr); with exit code 0, stdout is added to the message as context.
- Stop: when the agent is about to finish. Exit code 2 sends stderr back and the agent goes
  on (at most three times per message).

Any other non-zero exit code, or a timeout, is shown to the user and the step goes on. The
hook gets a JSON object on stdin (hook_event_name, session_id, cwd, tool_name, tool_input,
tool_response, prompt). The matcher is a regular expression on the tool name; Claude Code's
names (Write, Edit, Bash, Read, Grep, Glob, WebFetch, WebSearch, Task) match too. Commands run
in the system shell: sh on macOS and Linux, cmd.exe on Windows.

A project's hooks run only after the user approves that exact file with /hooks; changing the
file needs a new approval. The agent's writes to either hooks file always ask first.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
TIMEOUT = 60          # seconds, unless a hook sets its own
MAX_TIMEOUT = 600
MAX_OUTPUT = 4000     # characters of a hook's stdout or stderr that are kept
MAX_STOP_ROUNDS = 3
TRUST_KEY = "hooks_trusted"

# Claude Code's tool names, so its hooks match the same steps here.
CLAUDE_NAMES = {
    "write_file": "Write", "edit_file": "Edit", "run_command": "Bash", "read_file": "Read",
    "grep": "Grep", "glob": "Glob", "list_files": "LS", "web_fetch": "WebFetch",
    "web_search": "WebSearch", "subagent": "Task", "update_tasks": "TodoWrite",
}


@dataclass(frozen=True)
class Hook:
    event: str
    matcher: str
    command: str
    timeout: int
    scope: str   # "user" or "project"
    file: str


def user_file() -> Path:
    from vesper.onboarding import home

    return home() / "hooks.json"


def project_file(root: Path) -> Path | None:
    """The nearest `.vesper/hooks.json` at or above `root`, never at or above home."""
    home = Path.home().resolve()
    root = Path(root).resolve()
    for folder in (root, *root.parents):
        if folder == home or folder in home.parents:
            return None
        candidate = folder / ".vesper" / "hooks.json"
        if candidate.is_file():
            return candidate
    return None


def is_hooks_file(p: Path) -> bool:
    """Writing one of these would make Vesper run new commands: the agent always asks."""
    try:
        return p.resolve() == user_file().resolve() or (p.name == "hooks.json" and p.parent.name == ".vesper")
    except OSError:
        return False


def fingerprint(path: Path, text: str) -> str:
    return hashlib.sha256(f"{path.resolve()}\n{text}".encode()).hexdigest()[:32]


_cache: dict[str, tuple[float, list, str]] = {}  # path: (mtime, hooks, text)
_cache_lock = threading.Lock()


def _parse(path: Path, scope: str) -> tuple[list[Hook], str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return [], ""
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1], hit[2]
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return [], ""
    hooks = []
    table = data.get("hooks") if isinstance(data, dict) else None
    for event, groups in (table if isinstance(table, dict) else {}).items():
        if event not in EVENTS or not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = str(group.get("matcher") or "")
            for h in group.get("hooks") or []:
                if isinstance(h, dict) and h.get("type", "command") == "command" and str(h.get("command", "")).strip():
                    try:
                        timeout = max(1, min(int(h.get("timeout", TIMEOUT)), MAX_TIMEOUT))
                    except (TypeError, ValueError):
                        timeout = TIMEOUT
                    hooks.append(Hook(event, matcher, str(h["command"]), timeout, scope, str(path)))
    with _cache_lock:
        _cache[key] = (mtime, hooks, text)
    return hooks, text


def configured(root: Path) -> tuple[list[Hook], Path | None]:
    """Every hook that may run for this folder, and the project file still waiting for approval."""
    from vesper import permissions

    hooks, _ = _parse(user_file(), "user")
    waiting = None
    project = project_file(root)
    if project is not None:
        found, text = _parse(project, "project")
        if found:
            if fingerprint(project, text) in permissions.load_rules(TRUST_KEY):
                hooks = hooks + found
            else:
                waiting = project
    return hooks, waiting


def all_hooks(root: Path) -> list[Hook]:
    """Every hook in both files, approved or not, for /hooks to show."""
    hooks, _ = _parse(user_file(), "user")
    project = project_file(root)
    return hooks + (_parse(project, "project")[0] if project else [])


def approve(root: Path) -> Path | None:
    """Trust the project's hooks file as it is now. Returns it, or None when there is none."""
    from vesper import permissions

    project = project_file(root)
    if project is None:
        return None
    _, text = _parse(project, "project")
    permissions.save_rule(fingerprint(project, text), TRUST_KEY)
    return project


def matches(matcher: str, tool: str) -> bool:
    if not matcher or matcher == "*":
        return True
    try:
        rx = re.compile(matcher)
    except re.error:
        return False
    return any(rx.fullmatch(name) for name in (tool, CLAUDE_NAMES.get(tool, "")) if name)


def run(hook: Hook, payload: dict, cwd: Path) -> tuple[int | None, str, str]:
    """Run one hook. (exit code, stdout, stderr); the code is None if it could not finish."""
    posix = os.name == "posix"
    env = {**os.environ, "VESPER_PROJECT_DIR": str(cwd), "CLAUDE_PROJECT_DIR": str(cwd)}
    try:
        proc = subprocess.Popen(hook.command, shell=True, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=posix,
                                **({} if posix else {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}))
    except OSError as exc:
        return None, "", f"could not start: {exc.strerror or exc}"
    data = json.dumps(payload, ensure_ascii=False, default=str).encode()
    try:
        out, err = proc.communicate(input=data, timeout=hook.timeout)
    except subprocess.TimeoutExpired:
        try:  # the whole group: a shell's children would keep the pipes open
            if posix:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        except (OSError, ProcessLookupError):
            proc.kill()
        try:
            proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return None, "", f"did not finish within {hook.timeout} s and was stopped"
    except (OSError, ValueError) as exc:
        return None, "", f"failed: {exc}"

    def text(b: bytes) -> str:
        s = (b or b"").decode("utf-8", errors="replace").strip()
        return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + "\n... (cut)"

    return proc.returncode, text(out), text(err)


class Runner:
    """Runs the hooks for one conversation. Never raises: a broken hook is reported, not fatal."""

    def __init__(self, notice=None, session_id=lambda: ""):
        self.notice = notice or (lambda text: None)
        self.session_id = session_id
        self._told_waiting: set[str] = set()

    def _hooks(self, event: str, root: Path, tool: str | None = None) -> list[Hook]:
        try:
            hooks, waiting = configured(root)
        except Exception:  # noqa: BLE001
            return []
        if waiting is not None and str(waiting) not in self._told_waiting:
            self._told_waiting.add(str(waiting))
            self.notice(f"This project has hooks in {waiting} that are not approved yet, so they do not run. "
                        "Review them with /hooks in the Vesper terminal, then /hooks approve.")
        return [h for h in hooks if h.event == event and (tool is None or matches(h.matcher, tool))]

    def _run(self, event: str, root: Path, extra: dict, tool: str | None = None) -> list[tuple[Hook, int | None, str, str]]:
        results = []
        for hook in self._hooks(event, root, tool):
            payload = {"hook_event_name": event, "session_id": self.session_id(), "cwd": str(root), **extra}
            code, out, err = run(hook, payload, root)
            if code not in (0, 2):
                why = f"exit code {code}" if code is not None else err
                detail = (err or out).splitlines()[0][:200] if code is not None and (err or out) else ""
                self.notice(f"Hook `{hook.command[:80]}` ({event}): {why}{': ' + detail if detail else ''}")
            results.append((hook, code, out, err))
        return results

    @staticmethod
    def tool_input(fn, args: tuple) -> tuple[str, dict]:
        """The tool's name and arguments as a hook sees them."""
        import inspect

        name = fn.__name__
        try:
            bound = dict(inspect.signature(fn).bind(*args).arguments)
        except (TypeError, ValueError):
            bound = {"args": list(args)}
        if name == "call_mcp":  # mcp__server__tool, with the tool's own arguments
            raw = bound.get("args_json", "{}")
            try:
                tool_args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                tool_args = {"arguments": raw}
            return (f"mcp__{bound.get('server', '')}__{bound.get('tool', '')}",
                    tool_args if isinstance(tool_args, dict) else {})
        return name, bound

    def before_tool(self, root: Path, tool: str, args: dict) -> str | None:
        """A reason to refuse the step, or None to run it."""
        try:
            for _hook, code, out, err in self._run("PreToolUse", root, {"tool_name": tool, "tool_input": args}, tool):
                if code == 2:
                    return f"Error: a hook blocked this step: {err or out or 'no reason given'}"
        except Exception:  # noqa: BLE001
            return None
        return None

    def after_tool(self, root: Path, tool: str, args: dict, result: str) -> str:
        """Text to add to a successful step's result (feedback from exit-code-2 hooks)."""
        try:
            notes = [err or out for _hook, code, out, err in self._run(
                "PostToolUse", root, {"tool_name": tool, "tool_input": args, "tool_response": result[:20000]}, tool)
                if code == 2 and (err or out)]
        except Exception:  # noqa: BLE001
            return ""
        return "".join(f"\nA hook says: {n}" for n in notes)

    def prompt(self, root: Path, text: str) -> tuple[str | None, str]:
        """(why the message is blocked or None, context to add to it)."""
        context = []
        try:
            for _hook, code, out, err in self._run("UserPromptSubmit", root, {"prompt": text}):
                if code == 2:
                    return err or out or "a hook blocked this message", ""
                if code == 0 and out:
                    context.append(out)
        except Exception:  # noqa: BLE001
            return None, ""
        return None, "\n\n".join(context)

    def stop(self, root: Path, last: str) -> str | None:
        """Feedback that sends the agent back to work, or None to let it finish."""
        try:
            for _hook, code, out, err in self._run("Stop", root, {"last_message": last[:20000]}):
                if code == 2 and (err or out):
                    return err or out
        except Exception:  # noqa: BLE001
            return None
        return None
