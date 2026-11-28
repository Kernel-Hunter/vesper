"""Vesper in the terminal.

    vesper                      start a session in the current folder
    vesper -c                   continue the last conversation in this folder
    vesper -r [ID]              resume a conversation (pick from a list, or give its id)
    vesper "question"           ask once and print the answer (no prompts; edits are refused)
    vesper sessions             list saved conversations
    vesper logs [ID]            show what the agent did in a conversation
    vesper app                  open the desktop app

Inside a session: type a request, `/` for commands, `@path` to attach a file, `!cmd` to run
a shell command yourself, Shift+Tab to change how much Vesper may do without asking.

This file is the session loop. Looks live in `ui.py`, setup questions in `onboarding.py`,
the agent in `coder.py`, its tools in `workspace.py`, saved history in `store.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich import box
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from vesper import __version__
from vesper.onboarding import (
    ask_key,
    first_run,
    home,
    load_config,
    open_app,
    pick_model,
    provider_ready,
    save_config,
    trust_folder,
)
from vesper.ui import (
    ACCENT,
    ACCENT_2,
    ACCENT_3,
    AUTO_DIFF_LINES,
    ERR,
    FAINT,
    MODES,
    MUTED,
    OK,
    TOOL_LABELS,
    TEXT,
    WARN,
    FeedPrinter,
    Renderer,
    commit_line,
    apply_theme,
    banner,
    console,
    diff_panel,
    err_console,
    error_panel,
    blend,
    meter_parts,
    mode_color,
    git_branch,
    gradient,
    keycap,
    keys_line,
    theme_swatch,
    user_bubble,
    fmt_ago,
    fmt_cost,
    fmt_tokens,
    model_label,
    note,
    print_events,
    print_transcript,
    recap,
    recent_block,
    sessions_table,
    short_path,
    summarize_args,
    summarize_output,
    tail_path,
    tasks_block,
    tool_end_line,
    tool_start_line,
    turn_footer,
)

__all__ = ["main", "App", "console", "banner", "TOOL_LABELS", "summarize_args", "summarize_output",
           "ACCENT", "MUTED", "OK", "ERR", "WARN", "load_config"]

SLASH = {
    "/help": "show commands and shortcuts",
    "/cd": "go to another folder: /cd ../other-project",
    "/new": "start a new conversation (the old one is saved)",
    "/resume": "go back to an earlier conversation",
    "/sessions": "list saved conversations (/sessions rm N deletes one, /sessions rename N title)",
    "/logs": "what the agent did in this conversation (/logs full for details)",
    "/verbose": "transcript view: show thinking and full tool output while working (ctrl+o)",
    "/transcript": "print this whole conversation in detail: thinking, commands, edits, output",
    "/status": "model, folder, session and token use",
    "/plan": "plan mode on or off: Vesper reads and proposes a plan, you approve it, then it works",
    "/undo": "put files back to how they were before Vesper's last changes",
    "/git": "git safety net: /git auto on commits each turn; /git worktree gives this chat its own copy (merge, discard)",
    "/rewind": "go back to before one of your messages and edit it (/rewind N; can put files back too)",
    "/compact": "summarize the older part of this conversation to free context",
    "/theme": "change the colors: /theme lists them, /theme dusk picks one",
    "/fallback": "switch models by itself when one runs out of credits (/fallback on, /fallback off, /fallback list)",
    "/small": "a cheaper model for summaries and chat titles (/small ollama llama3.2, /small off)",
    "/check": "tests or lint that run after Vesper edits files; failures go back to it (/check pytest -q, /check off)",
    "/checkpoints": "files at each of your messages; /checkpoints N puts them back as they were then",
    "/export": "save this conversation as Markdown or HTML (/export html [path])",
    "/memory": "notes Vesper keeps between conversations (/memory forget N removes one)",
    "/hooks": "your commands that run before or after the agent's steps (/hooks approve trusts this project's)",
    "/diagnostics": "type errors from the language server after each edit go back to Vesper (/diagnostics on|off)",
    "/permissions": "commands and sites that run without asking (/permissions remove N)",
    "/mcp": "MCP servers and their tools (/mcp approve [name], /mcp restart [name])",
    "/think": "how hard reasoning models think: off | low | medium | high",
    "/model": "switch provider and model",
    "/key": "set the API key for the current provider",
    "/mode": "ask | auto-edit | yolo | plan",
    "/telegram": "use Vesper from your phone: /telegram sets it up or shows how it is doing; /telegram on, off, watch",
    "/app": "open the desktop app",
    "/exit": "quit (or ctrl+d)",
}
ALIASES = {"/clear": "/new", "/quit": "/exit", "/q": "/exit", "/history": "/sessions", "/delete": "/sessions rm", "/log": "/logs",
           "/allow": "/permissions", "/thinking": "/think"}
HELP_GROUPS = [
    ("Conversation", ["/new", "/resume", "/sessions", "/rewind", "/undo", "/checkpoints", "/compact", "/transcript", "/export", "/logs"]),
    ("Model", ["/model", "/key", "/small", "/fallback", "/think"]),
    ("How Vesper works", ["/plan", "/mode", "/check", "/diagnostics", "/hooks", "/memory", "/git", "/cd", "/permissions", "/mcp", "/status"]),
    ("Look", ["/theme", "/verbose"]),
    ("Elsewhere", ["/telegram", "/app"]),
]
SHORTCUTS = [
    ("@path", "attach a file, folder or image (@\"path with spaces\"); dropping an image file works too"),
    ("!command", "run a shell command yourself (!cd folder moves Vesper too)"),
    ("shift+tab", "cycle mode: ask → auto-edit → yolo → plan"),
    ("ctrl+o", "transcript view on or off (thinking and full tool output)"),
    ("esc then enter", "new line"),
    ("ctrl+c", "stop the answer · twice to quit"),
    ("↑ ↓", "previous messages"),
]
MAX_PIPED_CHARS = 200_000  # text piped into one-shot mode
STDIN_WAIT_S = 2.0         # how long one-shot mode waits for piped text when it already has a question
MENTION = re.compile(r"""(?<![\w/])@(?:"([^"]+)"|'([^']+)'|((?:[\w./~\-+]|\\ )+))""")
# Windows terminals paste C:\path or "C:\path with spaces"; macOS and Linux paste /path, '/path' or /path\ with\ spaces.
DROPPED = re.compile(r"""'((?:/|[A-Za-z]:\\)[^']+)'|"((?:/|[A-Za-z]:\\)[^"]+)"|([A-Za-z]:\\[^\s"']+)|((?:~|/)(?:[^\s\\]|\\ )+)""")


def mention_refs(text: str) -> list[str]:
    """Paths named with @: @src/app.py, @"a folder/with spaces", @my\\ file.txt."""
    refs = []
    for m in MENTION.finditer(text):
        ref = (m.group(1) or m.group(2) or m.group(3).replace("\\ ", " ").rstrip(".,;:"))
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def dropped_paths(text: str) -> list[str]:
    """Absolute paths a terminal pastes when a file is dragged onto it (quoted or with escaped spaces)."""
    found = []
    for m in DROPPED.finditer(text):
        raw = m.group(1) or m.group(2) or m.group(3) or m.group(4).replace("\\ ", " ")
        path = Path(raw).expanduser()
        if path.is_file():
            found.append(str(path))
    return found


def open_store():
    """The session database, or None when it cannot be opened (Vesper still works, without history)."""
    from vesper.store import Store

    try:
        return Store()
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"Session history is off: cannot open {home() / 'vesper.db'} ({exc}).", style=WARN)
        return None


def make_llm(cfg: dict):
    from vesper import providers as prov

    return prov.get_llm(cfg.get("provider"), cfg.get("model") or None, thinking=cfg.get("thinking") or "off")


def make_small_llm(cfg: dict):
    """The cheaper model for summaries and titles (None: the main model does them)."""
    from vesper import providers as prov

    try:
        return prov.get_small_llm(cfg.get("small_provider"), cfg.get("small_model"))
    except Exception:  # noqa: BLE001 - a broken small model falls back to the main one
        return None


def make_switcher(cfg: dict):
    """The fallback order when automatic switching is on (None when it is off)."""
    from vesper import fallback
    from vesper import providers as prov

    if not fallback.settings()["enabled"]:
        return None
    pid = cfg.get("provider") or "ollama"
    model = cfg.get("model") or (prov.PROVIDERS[pid].chat_model if pid in prov.PROVIDERS else "")
    return fallback.Switcher(lambda: fallback.chain_for(pid, model))


def context_tokens(cfg: dict) -> int:
    from vesper import providers as prov

    return prov.context_tokens(cfg.get("provider"), cfg.get("model"))


def friendly(exc: BaseException, cfg: dict) -> str:
    from vesper.server import friendly_error

    msg = friendly_error(exc, cfg.get("provider", ""))
    msg = msg.replace("Open Settings and paste a valid key", "Run /key to paste a valid key")
    return msg.replace("Pick another one in the model menu", "Run /model to pick another one")


# ---------------------------------------------------------------- the session
class App:
    """One terminal session. The agent runs in a worker thread; this thread draws and asks."""

    def __init__(self, root: Path, mode: str, cfg: dict, store=None) -> None:
        self.cfg = cfg
        self.mode = mode
        self.store = store
        self.mirror = None
        self.verbose = bool(cfg.get("verbose"))
        self.renderer = Renderer()
        self.workspace = None
        self.session = None
        self._start_root = root
        self._events: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._shown_cwd = root
        self._before_plan = "ask"   # the mode to go back to when a plan is approved
        self._prefill = ""          # text put into the next prompt (after /rewind)
        self._finished = False      # the last turn ran to its end (not stopped, no error)

    @property
    def root(self) -> Path:
        return self.workspace.root if self.workspace is not None else self._start_root

    def build(self, resume: dict | None = None) -> None:
        from vesper import permissions
        from vesper.workspace import Workspace

        self.workspace = Workspace(
            self._start_root, approve=self._approve_from_tool, mode=self.mode,
            on_auto=lambda action: self._events.put(("auto", action)),
            on_output=lambda line: self._events.put(("output", {"line": line})),
            rules=permissions.load_rules("allow_commands"), hosts=permissions.load_rules("allow_hosts"),
            on_rule=lambda key, rule: permissions.save_rule(rule, key),
        )
        self.new_session(resume)

    def new_session(self, resume: dict | None = None) -> None:
        from vesper.coder import Session

        self.session = Session(
            make_llm(self.cfg), self.workspace, self.store, session_id=resume["id"] if resume else None,
            source="terminal", provider=self.cfg.get("provider", ""), model=self.cfg.get("model", ""),
            context_tokens=context_tokens(self.cfg),
        )
        self.session.small_llm = make_small_llm(self.cfg)
        self.session.switcher = make_switcher(self.cfg)

    # ----- approvals -----
    def _approve_from_tool(self, action: dict) -> str:
        """Called by a tool, in the agent's thread: hand the question to the main thread and wait."""
        if self._stop.is_set():
            return "no"
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._events.put(("approval", {"action": action, "reply": reply}))
        return reply.get()

    def approve(self, action: dict) -> str:
        self.renderer.stop()
        strict, kind, rule = action.get("strict"), action["kind"], action.get("rule")
        if kind == "edit":
            title = ("Create " if action.get("new") else "Edit ") + action.get("path", "")
            console.print(diff_panel(action, title, ERR if strict else WARN))
            always = "always allow edits this session"
        elif kind == "web":
            console.print(Panel(Text(action.get("url", ""), style="bold"), title="Read a web page", title_align="left",
                                border_style=WARN, box=box.ROUNDED))
            always = f"always allow {rule}"
        elif kind == "mcp":
            body = Group(Text(f"{action.get('server')} / {action.get('mcp_tool')}", style="bold"),
                         Text(action.get("args", ""), style=MUTED))
            console.print(Panel(body, title="Use an MCP tool", title_align="left", border_style=WARN, box=box.ROUNDED))
            always = f"always allow {action.get('mcp_tool')}"
        else:
            body = Group(Text("$ " + action.get("command", ""), style="bold"),
                         Text("in " + short_path(action.get("cwd", self.root)), style=MUTED))
            console.print(Panel(body, title="Run a command", title_align="left",
                                border_style=ERR if strict else WARN, box=box.ROUNDED))
            always = f"always allow `{rule}` commands" if rule else "always allow this exact command this session"
        if strict:
            note("This can do serious damage or touches credentials, so it needs a yes every time.", ERR)
        try:
            while True:
                line = Text.assemble(("  Allow?   ", f"bold {TEXT}"))
                line += keycap("y", OK)
                line.append(" yes    ", style=MUTED)
                if not strict:
                    line += keycap("a", ACCENT)
                    line.append(f" {always}    ", style=MUTED)
                line += keycap("n", ERR)
                line.append(" no    ", style=MUTED)
                line.append(f"enter = {'no' if strict else 'yes'}  › ", style=FAINT)
                console.print(line, end="")
                answer = input().strip().lower()
                if answer in ("y", "yes") or (answer == "" and not strict):
                    return "yes"
                if answer in ("a", "always") and not strict:
                    return "always"
                if answer in ("n", "no", ""):
                    return "no"
        except (KeyboardInterrupt, EOFError):
            console.print()
            return "no"
        finally:
            self.renderer.restart("Working")

    # ----- one turn -----
    def run_turn(self, text: str, shown: str | None = None, images: list | None = None) -> None:
        self.wait_idle()
        self.renderer.new_stretch()
        events = self._events = queue.Queue()
        stop = self._stop = threading.Event()
        session = self.session
        self._finished = True

        def work() -> None:
            gen = session.ask(text, shown=shown, images=images)
            try:
                for item in gen:
                    if stop.is_set():
                        break
                    events.put(item)
            except BaseException as exc:  # noqa: BLE001 - shown to the user by the main thread
                events.put(("error", {"exc": exc}))
            finally:
                gen.close()
                events.put(("end", {}))

        self._worker = threading.Thread(target=work, daemon=True, name="vesper-turn")
        self._worker.start()
        r = self.renderer
        r.restart("Thinking")
        try:
            while True:
                try:
                    event, data = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if event == "end":
                    break
                self.draw(event, data)
        except KeyboardInterrupt:
            stop.set()
            self.workspace.cancel()
            self._finished = False
            r.end_thought(self.verbose)
            r.flush()
            note("⎿  Stopped. Tell Vesper what to do instead.", WARN)
        finally:
            r.flush()

    def draw(self, event: str, data: dict) -> None:
        r = self.renderer
        if event not in ("reasoning", "output"):
            r.end_thought(self.verbose)
        if event == "token":
            r.token(data["text"])
        elif event == "reasoning":
            r.flush()
            r.thought(data["text"])
        elif event == "tool_draft":  # the model is still writing the call: say which one, and how far
            r.flush()
            label = TOOL_LABELS.get(data["name"], data["name"])
            r.progress(f"Preparing {label} ({fmt_tokens(data['chars'])} characters)")
        elif event == "tasks":
            r.stop()
            console.print(tasks_block(data["items"]))
            r.start()
        elif event in ("tool_start", "tool_end") and data["name"] == "update_tasks" and data.get("ok", True):
            r.flush()  # the list itself is drawn by the "tasks" event
            r.restart("Thinking")
        elif event == "tool_start":
            r.flush()
            r.new_stretch()
            console.print(tool_start_line(data["name"], data["args"]))
            if self.verbose and data["name"] == "run_command":
                console.print(Text("     $ " + str(data["args"].get("command", "")), style=MUTED))
            r.restart(TOOL_LABELS.get(data["name"], data["name"]))
        elif event == "tool_end":
            r.stop()
            console.print(tool_end_line(data["name"], data["output"], data["ok"], data.get("ms"), self.verbose))
            r.restart("Thinking")
        elif event == "output":
            r.output(data["line"])
        elif event == "approval":
            data["reply"].put(self.approve(data["action"]))
        elif event == "auto":
            if data["kind"] == "edit":  # the edit did not need a yes; still show what changed
                r.stop()
                title = ("Created " if data.get("new") else "Edited ") + data.get("path", "")
                console.print(diff_panel(data, title, MUTED, None if self.verbose else AUTO_DIFF_LINES))
                r.start()
        elif event == "status":
            r.restart(data["text"])
        elif event == "cwd":
            if Path(data["path"]) != self._shown_cwd:
                self._shown_cwd = Path(data["path"])
                r.stop()
                note("→ now in " + tail_path(data["path"], 70), ACCENT_2)
                r.start()
        elif event == "notice":
            r.stop()
            note(data["text"], WARN)
            r.start()
        elif event == "commit":
            self._commit_event = data  # shown under the answer, with the footer
        elif event == "usage":
            r.flush()
            if getattr(self, "_commit_event", None):
                console.print(commit_line(self._commit_event))
                self._commit_event = None
            console.print(turn_footer(data, self.session.context_tokens))
        elif event == "error":
            r.flush()
            self._finished = False
            self.show_error(data["exc"])

    def wait_idle(self) -> None:
        """A stopped request may still be finishing its last model call; let it end first."""
        worker = self._worker
        if worker is None or not worker.is_alive():
            return
        with console.status(Text("Finishing the stopped request…", style=MUTED), spinner="vesper", spinner_style=f"bold {ACCENT_3}"):
            while worker.is_alive():
                try:
                    event, data = self._events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if event == "approval":
                    data["reply"].put("no")

    def show_error(self, exc: BaseException) -> None:
        error_panel(friendly(exc, self.cfg), "/model switches provider or model · /key sets the API key")

    # ----- input -----
    def _complete_path(self, frag: str, only_dirs: bool = False):
        """Entries that complete a path typed so far (relative to the working directory, absolute or ~)."""
        from prompt_toolkit.completion import Completion

        head, _, prefix = frag.rpartition("/")
        head = head + "/" if "/" in frag else ""
        try:
            base = self.workspace.resolve(head or ".")
            entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        shown = 0
        for p in entries:
            if not p.name.startswith(prefix) or (p.name.startswith(".") and not prefix.startswith(".")):
                continue
            if only_dirs and not p.is_dir():
                continue
            yield Completion(head + p.name + ("/" if p.is_dir() else ""), start_position=-len(frag))
            shown += 1
            if shown >= 200:
                return

    def make_prompt(self):
        from prompt_toolkit.output import ColorDepth

        def color_depth():
            # prompt_toolkit assumes 256 colors; most terminals that say so can show them all.
            if os.environ.get("PROMPT_TOOLKIT_COLOR_DEPTH"):
                return None
            return ColorDepth.TRUE_COLOR if os.environ.get("COLORTERM") in ("truecolor", "24bit") else None

        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import DynamicStyle, Style

        app = self

        class Complete(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if text.startswith("/") and " " not in text:
                    for cmd, desc in SLASH.items():
                        if cmd.startswith(text):
                            yield Completion(cmd, start_position=-len(text), display_meta=desc)
                    from vesper.commands import find_commands

                    for name, c in find_commands(app.workspace.root).items():
                        if "/" + name not in SLASH and ("/" + name).startswith(text):
                            yield Completion("/" + name, start_position=-len(text), display_meta=c["description"])
                    return
                moving = re.match(r"^(/cd|!cd)\s+(\S*)$", text)
                if moving:
                    yield from app._complete_path(moving.group(2), only_dirs=True)
                    return
                m = re.search(r"@([\w./~\-]*)$", text)
                if m:
                    yield from app._complete_path(m.group(1))

        kb = KeyBindings()

        @kb.add("s-tab")
        def _(event):
            order = list(MODES)
            app.set_mode(order[(order.index(app.mode) + 1) % len(order)], quiet=True)
            event.app.invalidate()

        @kb.add("c-r")
        def _(event):  # history: the conversation picker
            event.app.exit(result="/resume")

        @kb.add("c-o")
        def _(event):
            app.set_verbose(not app.verbose, quiet=True)
            event.app.invalidate()

        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        def toolbar():
            from prompt_toolkit.application import get_app

            cols = get_app().output.get_size().columns
            sep = [(f"fg:{FAINT}", "  │  " if cols >= 90 else " · ")]
            model = model_label(app.cfg).split(" · ", 1)[-1]
            used = app.session.tokens["context"] if app.session else 0
            share = min(1.0, used / app.session.context_tokens) if used else 0.0
            branch = git_branch(app.root)
            segments = [  # most important first; the rest are dropped when the window is narrow
                [(f"fg:{ACCENT_3} bold", "✦ "), (f"fg:{TEXT}", model)],
                [(f"fg:{mode_color(app.mode)} bold", app.mode)] + ([(f"fg:{FAINT}", " shift+tab")] if cols >= 100 else []),
                [(f"fg:{FAINT}", "context ")] + [(f"fg:{c}", piece) for piece, c in meter_parts(share, 8)]
                + [(f"fg:{MUTED}", f" {round(100 * share)}%")] if used else [],
                [(f"fg:{MUTED}", tail_path(app.root, 32))],
                [(f"fg:{MUTED}", f"⎇ {branch}")] if branch else [],
                [(f"fg:{ACCENT_2}", "transcript")] if app.verbose else [],
                [(f"fg:{FAINT}", "/help")],
            ]
            out, room = [("", " ")], cols - 2
            for seg in (seg for seg in segments if seg):
                size = sum(len(t) for _, t in seg) + (len(sep[0][1]) if len(out) > 1 else 0)
                if size > room:
                    continue
                out += (sep if len(out) > 1 else []) + seg
                room -= size
            return out

        def message():
            from prompt_toolkit.application import get_app

            cols = get_app().output.get_size().columns
            return [(f"fg:{FAINT}", "─" * max(10, cols - 1) + "\n"), (f"fg:{ACCENT} bold", "❯ ")]

        styles: dict[str, Style] = {}

        def style():
            if ACCENT not in styles:
                from vesper import ui

                dark = ui.THEME.dark
                menu = blend(FAINT, "#000000" if dark else "#ffffff", 0.55 if dark else 0.82)
                pick = blend(ACCENT, "#000000" if dark else "#ffffff", 0.45 if dark else 0.7)
                styles.clear()
                styles[ACCENT] = Style.from_dict({
                    "bottom-toolbar": "noreverse", "bottom-toolbar.text": "",
                    "completion-menu.completion": f"bg:{menu} fg:{TEXT}",
                    "completion-menu.completion.current": f"bg:{pick} fg:{TEXT} bold",
                    "completion-menu.meta.completion": f"bg:{menu} fg:{MUTED}",
                    "completion-menu.meta.completion.current": f"bg:{pick} fg:{TEXT}",
                    "scrollbar.background": f"bg:{menu}", "scrollbar.button": f"bg:{ACCENT}",
                })
            return styles[ACCENT]

        try:
            home().mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(home() / "history"))
        except OSError:
            history = InMemoryHistory()
        return PromptSession(
            message=message,
            history=history, completer=Complete(), complete_while_typing=True, key_bindings=kb,
            bottom_toolbar=toolbar, enable_history_search=False, erase_when_done=True,
            style=DynamicStyle(style), color_depth=color_depth(),
        )

    def set_mode(self, mode: str, quiet: bool = False) -> None:
        if mode == "plan" and self.mode != "plan":
            self._before_plan = self.mode
        self.mode = mode
        if self.workspace is not None:
            self.workspace.mode = mode
            self.workspace.always.clear()
        if mode != "plan":  # plan mode is for one piece of work, not a setting to start in
            self._remember({"agent_mode": mode})
        if not quiet:
            console.print(Text.assemble(("  mode: ", MUTED), (mode, "bold"), (f" · {MODES[mode]}", MUTED)))

    def set_verbose(self, on: bool, quiet: bool = False) -> None:
        self.verbose = on
        self._remember({"verbose": on})
        if not quiet:
            note("transcript view on: thinking and full tool output are shown." if on
                 else "transcript view off: thinking and tool output are summarized.")

    @staticmethod
    def _remember(patch: dict) -> None:
        try:
            save_config(patch)
        except OSError:
            pass

    def expand_mentions(self, text: str) -> tuple[str, list[dict]]:
        """`@path` attaches a file, a folder or an image; so does an image path dropped into the terminal.
        Returns (message text, image blocks)."""
        return attach_mentions(self.workspace, text, console)

    # ----- plan, undo, context, rules -----
    def offer_plan(self) -> bool:
        """After a plan-mode answer: ask whether to carry it out. Returns True when the user said yes."""
        try:
            console.print(Text.assemble(("  Carry out this plan?  ", "bold"), ("y", f"bold {OK}"), (" yes  ", MUTED),
                                        ("n", f"bold {ERR}"), (" keep planning  ", MUTED), ("[n] ", MUTED)), end="")
            answer = input().strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print()
            return False
        if answer not in ("y", "yes"):
            note("Still in plan mode. Say what to change, or /plan to leave it.")
            return False
        self.set_mode(self._before_plan)
        return True

    def undo(self) -> None:
        self.wait_idle()
        try:
            paths = self.session.undo()
        except OSError as exc:
            note(f"Could not put the files back: {exc}", ERR)
            return
        if not paths:
            note("Nothing to undo: Vesper has not changed a file in this conversation.")
            return
        for path in paths:
            console.print(Text.assemble(("  ↩ ", ACCENT_2), (self.workspace.rel(Path(path)), "")))
        note(f"{len(paths)} file{'s' if len(paths) != 1 else ''} put back. Commands Vesper ran are not undone.")
        if self.session.undo_note:
            note(self.session.undo_note)

    def rewind(self, arg: str = "") -> None:
        """/rewind [N]: forget one of the user's messages and everything after it; its text comes back to edit."""
        self.wait_idle()
        starts = self.session.user_turns()
        if not starts:
            note("Nothing to go back to yet.")
            return
        from vesper.coder import user_text

        texts = [user_text(self.session.history[i]) for i in reversed(starts[-10:])]
        if not arg.strip():
            console.print(Text("Go back to before which message? 1 is your latest.", style="bold"))
            for n, text in enumerate(texts, start=1):
                first = text.splitlines()[0] if text else ""
                console.print(Text.assemble((f"  {n:>2}  ", ACCENT), (first[:90] + ("…" if len(first) > 90 or "\n" in text else ""), "")))
            try:
                arg = input("  number [1]: ").strip() or "1"
            except (KeyboardInterrupt, EOFError):
                console.print()
                return
        if not arg.isdigit() or not 1 <= int(arg) <= len(starts):
            note(f"Give a number from 1 to {len(starts)}.", WARN)
            return
        try:
            files = input("  Also put back files Vesper changed since then? [y/N] ").strip().lower() in ("y", "yes")
        except (KeyboardInterrupt, EOFError):
            console.print()
            return
        try:
            out = self.session.rewind(int(arg) - 1, files=files)
        except (ValueError, OSError) as exc:
            note(f"Could not go back: {exc}", ERR)
            return
        for path in out["restored"]:
            console.print(Text.assemble(("  ↩ ", ACCENT_2), (self.workspace.rel(Path(path)), "")))
        if files and not out["files_known"]:
            note("Only the conversation went back: that message is older than file tracking.", WARN)
        note("Went back. Your message is in the prompt: edit it and press Enter, or clear it.")
        self._prefill = out["text"]

    def git(self, arg: str) -> None:
        from vesper import gitnet

        words = arg.lower().split()
        root = self.workspace.root
        repo = gitnet.repo_root(root)
        if words[:1] == ["auto"] and words[1:2] in (["on"], ["off"]):
            on = words[1] == "on"
            gitnet.set_enabled(root, on)
            if on and repo is None:
                note("Auto-commit is on for this folder, but it is not a git repository yet (git init).", WARN)
            elif on:
                why = gitnet.blocked(repo)
                note("After each turn that changes files, Vesper commits them with your git name and hooks. "
                     "Files you had already changed stay out. /undo takes the commit back too."
                     + (f"\n  Right now it cannot: {why}." if why else ""))
            else:
                note("Auto-commit is off for this folder.")
            return
        if words[:1] in (["worktree"], ["copy"]):
            self.git_worktree(arg.split(None, 1)[1] if len(arg.split(None, 1)) > 1 else "")
            return
        if words[:1] == ["merge"]:
            self.git_merge()
            return
        if words[:1] == ["discard"]:
            self.git_discard()
            return
        if words:
            note("Use /git, /git auto on|off, /git worktree [name], /git merge or /git discard.", WARN)
            return
        if repo is None:
            note("This folder is not in a git repository. With one, /git auto on commits each turn's changes.")
            return
        on = gitnet.enabled(root)
        branch = gitnet.git(repo, "branch", "--show-current")[1] or "(detached)"
        if (wt := gitnet.worktree_of(root)) is not None:
            console.print(Text.assemble(("  separate copy of ", FAINT), (str(wt["main"]), TEXT),
                                        (f"  from {wt['base'] or 'HEAD'}", MUTED)))
            console.print(Text("  /git merge brings it back · /git discard throws it away", style=MUTED))
        console.print(Text.assemble(("  repository  ", FAINT), (str(repo), TEXT), ("  ⎇ ", MUTED), (branch, ACCENT_2)))
        console.print(Text.assemble(("  auto-commit ", FAINT), ("on" if on else "off", f"bold {OK if on else MUTED}"),
                                    ("  /git auto " + ("off" if on else "on"), MUTED)))
        if on and (why := gitnet.blocked(repo)):
            console.print(Text(f"  cannot commit now: {why}", style=WARN))
        made = self.session._commits if self.session is not None else []
        for c in made[-5:]:
            console.print(Text.assemble(("    ", ""), (c["sha"][:7], f"bold {TEXT}"),
                                        (f"  {len(c['files'])} file{'s' if len(c['files']) != 1 else ''}", MUTED)))

    def git_worktree(self, name: str) -> None:
        from vesper import gitnet

        self.wait_idle()
        if gitnet.worktree_of(self.root) is not None:
            note("This chat already works in a separate copy. /git merge or /git discard ends it.", WARN)
            return
        with console.status("Making a separate copy of the repository…"):
            path, branch, notes = gitnet.start_worktree(self.root, name)
        if path is None:
            note(notes[0] if notes else "Could not make a copy.", ERR)
            return
        self.workspace.change_directory(str(path))
        self._shown_cwd = self.root
        note(f"→ this chat now works in its own copy, on branch {branch}:", ACCENT_2)
        note("  " + tail_path(path, 80))
        note("Other chats and the main checkout are not touched. /git merge brings the work back when it is done; "
             "/git discard throws it away.")
        for line in notes:
            note(line, WARN)

    def git_merge(self) -> None:
        from vesper import gitnet

        self.wait_idle()
        info = gitnet.worktree_of(self.root)
        if info is None:
            note("This chat is not in a separate copy. /git worktree makes one.", WARN)
            return
        title = (self.session.title if self.session is not None else "") or "Work from " + info["branch"]
        with console.status("Merging…"):
            merged, said = gitnet.merge_worktree(info, gitnet.subject(title))
        note(said, OK if merged else WARN)
        if merged:
            self.workspace.change_directory(str(info["main"]))
            self._shown_cwd = self.root
            note("→ back in " + tail_path(self.root, 70), ACCENT_2)

    def git_discard(self) -> None:
        from vesper import gitnet
        from vesper.telegram import _ask

        self.wait_idle()
        info = gitnet.worktree_of(self.root)
        if info is None:
            note("This chat is not in a separate copy.", WARN)
            return
        files, commits = gitnet.discard_counts(info)
        lost = []
        if files:
            lost.append(f"{files} changed file{'s' if files != 1 else ''}")
        if commits:
            lost.append(f"{commits} commit{'s' if commits != 1 else ''} not merged")
        note(f"Discarding deletes the copy and branch {info['branch']}"
             + (f", with {' and '.join(lost)}." if lost else " (nothing in it is lost).")
             + " This cannot be undone.", WARN)
        answer = _ask("  Discard it? [y/N] ")
        if (answer or "").strip().lower() not in ("y", "yes"):
            note("Kept.")
            return
        self.workspace.change_directory(str(info["main"]))
        self._shown_cwd = self.root
        gitnet.remove_worktree(info, force=True)
        note("Discarded. → back in " + tail_path(self.root, 70), ACCENT_2)

    def check(self, arg: str) -> None:
        from vesper import checks

        root = self.workspace.root
        try:
            if arg.lower() in ("off", "none", "clear"):
                checks.set_command(root, "")
                note("Checks are off for this folder.")
                return
            if arg:
                checks.set_command(root, arg)
                note(f"After Vesper edits files here, it runs `{arg}` and fixes what fails (up to {checks.MAX_ROUNDS} tries).")
                return
        except ValueError as exc:
            note(str(exc))
            return
        current, guess = checks.command_for(root), checks.suggest(root)
        if current:
            note(f"Check command: `{current}`. Change it with /check <command>, turn it off with /check off.")
        else:
            note("No check command for this folder." + (f" Try: /check {guess}" if guess else " Set one with /check <command>, for example /check pytest -q."))

    def checkpoints(self, arg: str = "") -> None:
        """/checkpoints [N]: the turns that changed files; N puts the files back to before that turn."""
        import datetime as dt

        self.wait_idle()
        entries = self.session.checkpoints()
        if not entries:
            note("No checkpoints yet: Vesper has not changed any files in this conversation.")
            return
        if not arg.strip():
            console.print(Text("Checkpoints, newest first. Files as they were before each message:", style="bold"))
            for n, e in enumerate(entries, start=1):
                first = (e["text"].splitlines() or ["(an earlier message)"])[0]
                when = dt.datetime.fromtimestamp(e["time"] / 1000).strftime("%H:%M") if e["time"] > 10**11 else ""
                console.print(Text.assemble((f"  {n:>2}  ", ACCENT), (f"{when}  " if when else "", FAINT),
                                            (first[:80] + ("…" if len(first) > 80 else ""), "")))
                for f in e["files"][:6]:
                    sign = {"added": "A", "deleted": "D"}.get(f["status"], "M")
                    console.print(Text.assemble(("        " + sign + " ", MUTED), (self.workspace.rel(Path(f["path"])), ""),
                                                (f"  +{f['add']}", OK), (f" −{f['del']}", ERR)))
                if len(e["files"]) > 6:
                    console.print(Text(f"        and {len(e['files']) - 6} more", style=FAINT))
            note("/checkpoints N puts the files back as they were before message N; the conversation stays. "
                 "/rewind also takes the conversation back.")
            return
        if not arg.strip().isdigit() or not 1 <= int(arg) <= len(entries):
            note(f"Give a number from 1 to {len(entries)}.", WARN)
            return
        entry = entries[int(arg) - 1]
        count = len({f["path"] for e in entries[:int(arg)] for f in e["files"]})
        try:
            sure = input(f"  Put {count} file{'s' if count != 1 else ''} back as they were before that message? [y/N] ")
        except (KeyboardInterrupt, EOFError):
            console.print()
            return
        if sure.strip().lower() not in ("y", "yes"):
            return
        try:
            paths = self.session.restore_checkpoint(entry["turn"])
        except (ValueError, OSError) as exc:
            note(f"Could not put the files back: {exc}", ERR)
            return
        for path in paths:
            console.print(Text.assemble(("  ↩ ", ACCENT_2), (self.workspace.rel(Path(path)), "")))
        note("Files are back. Vesper will be told before your next message.")

    def export(self, arg: str) -> None:
        """/export [md|html] [path]: write this conversation to a file (default: here, Markdown)."""
        from vesper import export

        words = arg.split(maxsplit=1)
        fmt = "md"
        if words and words[0].lower().lstrip(".") in ("md", "markdown", "html"):
            fmt = "html" if words[0].lower().lstrip(".") == "html" else "md"
            words = words[1:]
        if not self.session.history:
            note("Nothing to export yet: this conversation is empty.")
            return
        row = (self.store.get_session(self.session.id) if self.store is not None else None) or {
            "id": self.session.id, "title": self.session.title or "", "cwd": str(self.workspace.root),
            "model": self.cfg.get("model", ""), "created_at": None}
        target = Path(os.path.expanduser(words[0])) if words else self.workspace.root
        if not target.is_absolute():
            target = self.workspace.root / target
        try:
            path = export.write(row, self.session.history, fmt, target)
        except (OSError, ValueError) as exc:
            note(f"Could not export: {exc}")
            return
        note(f"Saved {path}")

    def memory(self, arg: str) -> None:
        from vesper import memory

        root = self.workspace.root
        words = arg.split()
        if words[:1] == ["forget"]:
            if len(words) < 2 or not words[1].isdigit():
                note("Which one? /memory forget N, with N from /memory.")
                return
            gone = memory.forget(root, int(words[1]))
            note(f"Forgot: {gone}" if gone else f"There is no note {words[1]}. See /memory.")
            return
        listed = memory.listing(root)
        if not listed:
            note("No notes yet. Vesper saves lasting facts with its remember tool (it asks first); "
                 "you can also say \"remember that ...\".")
            return
        rows = [f"{i:>3}. {'you' if scope == 'user' else 'project':<8}{text}" for i, (scope, text) in enumerate(listed, 1)]
        note("Memory:\n" + "\n".join(rows) + "\n  /memory forget N removes one. Files: " + str(memory.folder()))

    def hooks(self, arg: str) -> None:
        from vesper import hooks

        root = self.workspace.root
        if arg.strip().lower() == "approve":
            approved = hooks.approve(root)
            note(f"Approved the hooks in {approved}. They run from now on; a change to the file needs a new approval."
                 if approved else "This project has no .vesper/hooks.json.")
            return
        _, waiting = hooks.configured(root)
        found = hooks.all_hooks(root)
        if not found:
            note(f"No hooks. Add them to {hooks.user_file()} (yours) or .vesper/hooks.json (this project), "
                 'in Claude Code\'s format: {"hooks": {"PostToolUse": [{"matcher": "edit_file", '
                 '"hooks": [{"type": "command", "command": "..."}]}]}}')
            return
        rows = [f"{h.scope:<8}{h.event:<17}{(h.matcher or '*')[:24]:<26}{h.command[:70]}" for h in found]
        note("Hooks:\n  " + "\n  ".join(rows))
        if waiting:
            note(f"The project's hooks in {waiting} do not run until you approve them: /hooks approve")

    def diagnostics(self, arg: str) -> None:
        from vesper import diagnostics

        word = arg.strip().lower()
        if word in ("on", "off"):
            diagnostics.set_enabled(word == "on")
            note("Diagnostics are on: after each edit, the language server's new errors go back to Vesper."
                 if word == "on" else "Diagnostics are off. Language servers are stopped.")
            return
        state = "on" if diagnostics.enabled() else "off"
        rows = diagnostics.status(self.workspace.root)
        found = [f"{name} ({how})" for name, how in rows if how != "not installed"]
        missing = [name for name, how in rows if how == "not installed"]
        note(f"Diagnostics are {state}. " + (f"Language servers: {', '.join(found)}. " if found else
             "No language server is installed for this folder. ")
             + (f"Not installed: {', '.join(missing)}. " if missing else "")
             + f"Turn {'off' if state == 'on' else 'on'} with /diagnostics {'off' if state == 'on' else 'on'}.")

    def vesper_command(self, line: str) -> None:
        """`vesper ...` typed at Vesper's own prompt: do the obvious thing instead of asking the model."""
        words = line.split()[1:]
        if words[:1] == ["telegram"]:
            rest = words[1:]
            arg = ("off" if "--stop" in rest else "on" if "--background" in rest else
                   " ".join(w for w in rest if not w.startswith("-")))
            self.telegram(arg)
            return
        note("You are already inside Vesper, so just type what you want done. Commands that start with "
             "`vesper` are for a normal terminal window; to run one from here, put ! in front: !" + line)

    def telegram(self, arg: str) -> None:
        from vesper import telegram as tg

        words = arg.split()
        action = words[0].lower() if words else ""
        if action in ("", "status"):
            state = tg.status(check_token=False)
            if not action and not (state["configured"] and state["paired"]):
                action = "setup"  # nothing to show yet: start the guided setup
            else:
                tg.show_status(console)
                return
        if action == "setup":
            if not self.cfg.get("provider") and not os.environ.get("VESPER_FAKE_LLM"):
                note("Pick a model first (/model): the bot uses the same one.")
                return
            tg.setup(tg._ask, tg._confirm, tg.printer(console), self.root)
        elif action in ("on", "start"):
            state = tg.status(check_token=False)
            if not (state["configured"] and state["paired"]):
                note("It is not set up yet. Type /telegram setup.")
                return
            with console.status("Starting the Telegram bot…"):
                problem = tg.start_service(self.root)
            note(problem or "The Telegram bot is on. It runs in the background and starts again when you log in.")
        elif action in ("off", "stop"):
            with console.status("Stopping the Telegram bot…"):
                tg.stop_service()
            note("The Telegram bot is off.")
        elif action == "watch":
            tg.watch(console)
        elif action == "mirror" and len(words) == 2 and words[1].lower() in ("on", "off"):
            on = words[1].lower() == "on"
            self._remember({"telegram_mirror": on})
            if self.mirror is not None:
                self.mirror.enabled = on
            note("Telegram activity shows here while Vesper waits for you." if on else
                 "Telegram activity no longer shows here. /telegram watch still follows it.")
        elif action == "approve" and len(words) == 2:
            who = tg.approve_code(words[1])
            if not who:
                note("No pairing request has that code (codes last one hour).")
                return
            note(f"Paired with {who['name']}. They can use the bot now.")
            with contextlib.suppress(Exception):
                tg.Api(os.environ.get(tg.TOKEN_KEY, "")).call(
                    "sendMessage", chat_id=who["chat"], text="✨ Paired. Send a message to start.")
        else:
            note("Use /telegram (status or setup), /telegram on, /telegram off, /telegram watch, "
                 "/telegram mirror on|off, /telegram approve CODE.")

    def theme(self, arg: str) -> None:
        from vesper import themes, ui
        from vesper.onboarding import save_config

        name = arg.strip().lower()
        if not name:
            console.print()
            light = themes.background_is_light()
            for p in themes.THEMES.values():
                if light is not None and p.dark == light:
                    continue  # made for the other kind of background: unreadable here
                if light is None and p.name in themes.VARIANTS and p.dark:
                    continue  # one line per theme when the background is unknown
                console.print(theme_swatch(p, p.name == ui.THEME.name))
            console.print()
            note("/theme <name> switches the colors and keeps them for next time.")
            return
        if name not in themes.THEMES:
            note(f"There is no theme called {name}. /theme lists them.", WARN)
            return
        chosen = apply_theme(name)
        self.cfg["theme"] = name
        save_config({"theme": name})
        if themes.swapped_from is not None:
            note(f"{themes.THEMES[name].label} is made for {'light' if themes.THEMES[name].dark is False else 'dark'} "
                 f"terminals and would be hard to read in this one, so this window uses {chosen.label}. "
                 f"It is saved and shows in a {'light' if not themes.THEMES[name].dark else 'dark'} terminal.", WARN)
            return
        console.print(Text("  ") + gradient(f"✦ {chosen.label}") + Text(f"  {chosen.mood}", style=MUTED))

    def fallback(self, arg: str) -> None:
        from vesper import fallback

        word = arg.strip().lower()
        if word in ("on", "off"):
            fallback.save(enabled=word == "on")
            self.session.switcher = make_switcher(self.cfg)
            note("Automatic switching is on: when a model runs out of credits or hits a limit, Vesper moves to the "
                 "next one and keeps working. /fallback list shows the order." if word == "on"
                 else "Automatic switching is off: an out-of-credits error stops the answer.")
            return
        if word not in ("", "list"):
            note("Use /fallback on, /fallback off or /fallback list.")
            return
        state = "on" if fallback.settings()["enabled"] else "off"
        if word == "":
            note(f"Automatic switching is {state}. /fallback list shows the order; /fallback on or off changes it.")
            return
        note("Reading your providers' model lists…")
        pid = self.cfg.get("provider") or "ollama"
        order = fallback.chain_for(pid, self.cfg.get("model") or "")
        if not order:
            note("No models to fall back on yet: add an API key with /key, or start Ollama.", WARN)
            return
        for i, e in enumerate(order, 1):
            console.print(f"  {i:>2}. {e['label']:<22} {e['model']}  [{e['why']}]")
        note(f"Automatic switching is {state}.")

    def small(self, arg: str) -> None:
        from vesper import providers as prov

        parts = arg.split(maxsplit=1)
        if parts and parts[0].lower() in ("off", "none", "clear"):
            patch = {"small_provider": "", "small_model": ""}
        elif len(parts) == 2 and parts[0].lower() in prov.PROVIDERS:
            patch = {"small_provider": parts[0].lower(), "small_model": parts[1].strip()}
        elif parts:
            note(f"Use /small PROVIDER MODEL, for example /small ollama llama3.2. Providers: {', '.join(prov.PROVIDERS)}.")
            return
        else:
            now = self.cfg.get("small_model") and f"{self.cfg.get('small_provider')} {self.cfg['small_model']}"
            note(f"Small model: {now}. Turn it off with /small off." if now else
                 "No small model: the main model writes summaries and titles. Set one with /small PROVIDER MODEL.")
            return
        self.cfg.update(patch)
        save_config(patch)
        self.session.small_llm = make_small_llm(self.cfg)
        note(f"Summaries and chat titles now use {patch['small_provider']} {patch['small_model']}." if patch["small_model"]
             else "Small model off: the main model writes summaries and titles.")

    def compact(self) -> None:
        self.wait_idle()
        before = self.session.tokens["context"]
        try:
            with console.status(Text("Summarizing the conversation…", style=MUTED), spinner="vesper", spinner_style=f"bold {ACCENT_3}"):
                done = self.session.compact()
        except KeyboardInterrupt:
            note("stopped", WARN)
            return
        if done:
            note(f"Older turns are now a summary: about {fmt_tokens(before)} → {fmt_tokens(self.session.tokens['context'])} tokens.")
        else:
            note("Nothing to summarize yet." if len(self.session.history) < 3 else "The model could not write a summary; nothing changed.", WARN)

    def mcp(self, arg: str) -> None:
        from vesper import mcp

        root = str(self.workspace.root)
        words = arg.split()
        if words and words[0] == "approve":
            names = mcp.approve(root, words[1] if len(words) > 1 else None)
            if not names:
                note("Nothing to approve: every server of this folder is approved already.", MUTED)
                return
            mcp.manager().for_root(root)
            note(f"Approved: {', '.join(names)}. Starting; the tools are ready for your next message.", OK)
            return
        if words and words[0] == "restart":
            mcp.manager().restart(root, words[1] if len(words) > 1 else None)
            note("Restarting. The tools are ready for your next message.", OK)
            return
        rows = mcp.status(root)
        t = Table(box=None, padding=(0, 2), show_header=False)
        t.add_column(style="bold")
        t.add_column()
        t.add_column(style=MUTED)
        colors = {"ready": OK, "failed": ERR, "needs approval": WARN}
        for r in rows:
            detail = f"{len(r['tools'])} tools" if r["status"] == "ready" else (r["error"] or "")
            t.add_row(r["name"], Text(r["status"], style=colors.get(r["status"], MUTED)),
                      f"{r['scope']} · {r['transport']} · {detail}".rstrip(" ·"))
        if not rows:
            t.add_row("", "No MCP servers yet.", "")
        console.print(Panel(t, title="MCP servers", title_align="left", border_style=ACCENT, box=box.ROUNDED, expand=False))
        for problem in mcp.problems(root):
            note(problem, WARN)
        note(f"Add servers in {short_path(str(mcp.user_config_path()))} or a project's .mcp.json "
             "(the same format as other harnesses) · /mcp approve · /mcp restart")

    def permissions(self, arg: str) -> None:
        from vesper import permissions as perm

        ws = self.workspace
        rows = [("allow_commands", r, ws.rules) for r in ws.rules] + [("allow_hosts", r, ws.hosts) for r in ws.hosts]
        words = arg.split()
        if words and words[0] in ("remove", "rm", "delete"):
            picked = int(words[1]) if len(words) > 1 and words[1].isdigit() else 0
            if not 1 <= picked <= len(rows):
                note("/permissions remove NUMBER (see /permissions for the numbers)", WARN)
                return
            key, rule, live = rows[picked - 1]
            live.remove(rule)
            try:
                perm.remove_rule(rule, key)
            except OSError:
                pass
            note(f"Removed: {rule}")
            return
        t = Table(box=None, padding=(0, 2), show_header=False)
        t.add_column(style=MUTED, justify="right")
        t.add_column()
        t.add_column(style=MUTED)
        for n, (key, rule, _) in enumerate(rows, 1):
            t.add_row(str(n), rule, "command" if key == "allow_commands" else "web site")
        if not rows:
            t.add_row("", "No saved rules yet.", "")
        console.print(Panel(t, title="runs without asking", title_align="left", border_style=ACCENT, box=box.ROUNDED, expand=False))
        note("Answer `a` to an approval to add a rule · /permissions remove NUMBER deletes one")
        note("Read-only commands never ask: " + ", ".join(perm.SAFE_COMMANDS[:12]) + ", …")

    def think(self, arg: str) -> None:
        from vesper import providers as prov

        level = {"on": "medium"}.get(arg, arg)
        if level not in prov.THINKING:
            note(f"thinking: {self.cfg.get('thinking') or 'off'} · /think off | low | medium | high")
            return
        self.cfg["thinking"] = level
        self._remember({"thinking": level})
        self._reload_llm()
        note("thinking: " + level + (" · the model decides by itself" if level == "off"
                                     else " · needs a model that can reason; ctrl+o shows the thinking"))

    # ----- moving around -----
    def cd(self, target: str) -> None:
        from vesper.workspace import ToolError

        if not target.strip():
            note(short_path(self.root))
            return
        try:
            self.workspace.change_directory(target.strip())
        except ToolError as exc:
            note(str(exc), ERR)
            return
        self._shown_cwd = self.root
        note("→ now in " + tail_path(self.root, 70), ACCENT_2)

    # ----- history -----
    def show_recent(self) -> None:
        if self.store is not None:
            recent_block([r for r in self.store.list_sessions(4) if r["id"] != getattr(self.session, "id", "")][:3])

    def show_sessions(self) -> list[dict]:
        rows = self.store.list_sessions(15) if self.store is not None else []
        if not rows:
            note("No saved conversations yet.")
        else:
            console.print(sessions_table(rows, self.session.id))
        return rows

    def manage_sessions(self, arg: str) -> None:
        words = arg.split(maxsplit=2)
        if not words:
            if self.show_sessions():
                note("/resume N goes back to one · /sessions rm N deletes one · /sessions rename N title")
            return
        if self.store is None:
            note("Session history is off.", WARN)
            return
        action, refs = words[0].lower(), words[1:]
        rows = self.store.list_sessions(15)

        def find(ref: str) -> dict | None:
            row = rows[int(ref) - 1] if ref.isdigit() and 1 <= int(ref) <= len(rows) else self.store.get_session(ref)
            if row is None:
                note(f"No conversation matches {ref!r}. /sessions lists them.", WARN)
            return row

        if action in ("rm", "delete", "del", "remove") and refs:
            targets = [find(r) for r in " ".join(refs).split()]
            if None in targets:
                return
            for row in targets:
                self.store.delete_session(row["id"])
                if row["id"] == self.session.id:
                    self.new_session()
                note(f"Deleted {row['title'] or row['id']!r}.")
        elif action == "rename" and len(refs) == 2:
            row = find(refs[0])
            if row is not None:
                if row["id"] == self.session.id:
                    self.session.rename(refs[1])
                else:
                    self.store.update_session(row["id"], title=refs[1])
                note(f"Renamed to {' '.join(refs[1].split())[:80]!r}.")
        else:
            note("Use /sessions, /sessions rm N [N...], or /sessions rename N new title.", WARN)

    def resume(self, ref: str) -> None:
        if self.store is None:
            note("Session history is off.", WARN)
            return
        ref = ref.strip()
        rows = self.store.list_sessions(15)
        if not ref and sys.stdin.isatty():
            row = self.pick_session()
            if row is not None:
                self.open_session(row)
            return
        if not ref:
            if not self.show_sessions():
                return
            try:
                ref = input("  number or id (enter to cancel): ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print()
                return
            if not ref:
                return
        row = rows[int(ref) - 1] if ref.isdigit() and 1 <= int(ref) <= len(rows) else self.store.get_session(ref)
        if row is None:
            note(f"No conversation matches {ref!r}. /sessions lists them.", WARN)
            return
        self.open_session(row)

    def pick_session(self) -> dict | None:
        """Every saved conversation in a list you move through with the arrow keys and search."""
        from vesper.picker import Item, choose

        rows = [r for r in self.store.list_sessions(200) if r["id"] != self.session.id]
        if not rows:
            note("No other saved conversations yet. They show up here after you talk to Vesper.")
            return None
        items = [Item(r["title"] or "(untitled)",
                      f"{fmt_ago(r['updated_at'])} · {tail_path(r['cwd'], 26)} · {r.get('messages', 0)} msgs",
                      r["id"] + " " + r["cwd"]) for r in rows]
        pick = choose(items, "Your conversations")
        return rows[pick] if pick is not None else None

    def open_session(self, row: dict) -> None:
        self.wait_idle()
        folder = Path(row["cwd"])
        if folder.is_dir() and folder != self.root:
            self.workspace.change_directory(str(folder))
        elif not folder.is_dir():
            note(f"Its folder {short_path(folder)} is gone; staying in {short_path(self.root)}.", WARN)
        self.new_session(row)
        self._shown_cwd = self.root
        recap(self.session, row)

    def show_logs(self, arg: str) -> None:
        words = arg.split()
        limit = next((int(w) for w in words if w.isdigit()), 40)
        events = self.store.events(self.session.id, limit) if self.store is not None else []
        if not events:
            note("Nothing has happened in this conversation yet.")
            return
        print_events(events, full="full" in words or self.verbose)
        note(f"session {self.session.id} · stored in {tail_path(self.store.path, 50)}")

    def show_status(self) -> None:
        s = self.session
        t = Table.grid(padding=(0, 2))
        t.add_column(style=MUTED, justify="right")
        t.add_column()
        t.add_row("version", __version__)
        t.add_row("model", model_label(self.cfg))
        t.add_row("folder", tail_path(self.root, 70))
        t.add_row("mode", f"{self.mode} · {MODES[self.mode]}")
        t.add_row("thinking", self.cfg.get("thinking") or "off")
        t.add_row("session", f"{s.id} · {len(s.history)} messages" + (f" · {s.title}" if s.title else ""))
        cached = f" ({fmt_tokens(s.tokens['cached'])} read from cache)" if s.tokens.get("cached") else ""
        t.add_row("tokens", f"{fmt_tokens(s.tokens['input'])} in{cached}, {fmt_tokens(s.tokens['output'])} out")
        if s.tokens.get("cost") is not None and "cost" in s.tokens:
            t.add_row("cost", f"about {fmt_cost(s.tokens['cost'])} at list prices")
        t.add_row("context", f"{fmt_tokens(s.tokens['context'])} of {fmt_tokens(s.context_tokens)} "
                             f"({min(100, round(100 * s.tokens['context'] / s.context_tokens))}%)")
        t.add_row("history", tail_path(self.store.path, 70) if self.store is not None else "off")
        console.print(Panel(t, title="status", title_align="left", border_style=ACCENT, box=box.ROUNDED, expand=False))

    # ----- slash commands -----
    def command(self, line: str) -> bool:
        """Runs a slash command. Returns False to quit."""
        cmd, _, arg = line.strip().partition(" ")
        cmd = ALIASES.get(cmd.lower(), cmd.lower())
        arg = arg.strip()
        if cmd == "/exit":
            return False
        if cmd == "/help":
            from vesper.commands import find_commands

            listed: set[str] = set()
            console.print()
            for group, names in HELP_GROUPS:
                console.print(Text.assemble(("  ", ""), (group, f"bold {TEXT}")))
                t = Table.grid(padding=(0, 3))
                t.add_column(style=f"bold {ACCENT}", min_width=14, no_wrap=True)
                t.add_column(style=MUTED)
                for c in names:
                    if c in SLASH:
                        t.add_row(c, Text(SLASH[c]))  # Text: "[name]" is not markup
                        listed.add(c)
                console.print(Padding(t, (0, 0, 1, 4)))
            rest = [(c, d) for c, d in SLASH.items() if c not in listed]
            custom = [(f"/{n}" + (f" {c['hint']}" if c["hint"] else ""), f"{c['description']} ({c['scope']})")
                      for n, c in find_commands(self.workspace.root).items() if "/" + n not in SLASH]
            if rest or custom:
                console.print(Text("  More", style=f"bold {TEXT}"))
                t = Table.grid(padding=(0, 3))
                t.add_column(style=f"bold {ACCENT}", min_width=14, no_wrap=True)
                t.add_column(style=MUTED)
                for c, d in rest + custom:
                    t.add_row(Text(c), Text(d))
                console.print(Padding(t, (0, 0, 1, 4)))
            console.print(Text("  Keys", style=f"bold {TEXT}"))
            t = Table.grid(padding=(0, 3))
            t.add_column(min_width=14, no_wrap=True)
            t.add_column(style=MUTED)
            for k, d in SHORTCUTS:
                t.add_row(keycap(k, ACCENT), d)
            console.print(Padding(t, (0, 0, 1, 4)))
        elif cmd == "/cd":
            self.cd(arg)
        elif cmd == "/new":
            self.wait_idle()
            self.session.clear()
            console.clear()
            banner(self.root, self.cfg, self.mode)
            self.show_recent()
        elif cmd == "/resume":
            self.resume(arg)
        elif cmd == "/sessions":
            self.manage_sessions(arg)
        elif cmd == "/logs":
            self.show_logs(arg)
        elif cmd == "/verbose":
            self.set_verbose(not self.verbose)
        elif cmd == "/transcript":
            from vesper.coder import transcript

            if self.session.history:
                print_transcript(transcript(self.session.history))
            else:
                note("Nothing has happened in this conversation yet.")
        elif cmd == "/status":
            self.show_status()
        elif cmd == "/mode":
            if arg in MODES:
                self.set_mode(arg)
            else:
                note("/mode ask | auto-edit | yolo | plan")
        elif cmd == "/plan":
            self.set_mode(self._before_plan if self.mode == "plan" else "plan")
        elif cmd == "/undo":
            self.undo()
        elif cmd == "/git":
            self.git(arg)
        elif cmd == "/rewind":
            self.rewind(arg)
        elif cmd == "/compact":
            self.compact()
        elif cmd == "/check":
            self.check(arg)
        elif cmd == "/diagnostics":
            self.diagnostics(arg)
        elif cmd == "/hooks":
            self.hooks(arg)
        elif cmd == "/memory":
            self.memory(arg)
        elif cmd == "/export":
            self.export(arg)
        elif cmd in ("/checkpoints", "/timeline"):
            self.checkpoints(arg)
        elif cmd == "/small":
            self.small(arg)
        elif cmd == "/fallback":
            self.fallback(arg)
        elif cmd == "/theme":
            self.theme(arg)
        elif cmd == "/telegram":
            self.telegram(arg)
        elif cmd == "/permissions":
            self.permissions(arg)
        elif cmd == "/mcp":
            self.mcp(arg)
        elif cmd == "/think":
            self.think(arg.lower())
        elif cmd == "/model":
            if pick_model(self.cfg):
                self._reload_llm()
        elif cmd == "/key":
            if ask_key(self.cfg.get("provider", "")):
                self._reload_llm()
        elif cmd == "/app":
            open_app()
        else:
            from vesper.commands import expand

            prompt = expand(self.workspace.root, line.strip(), set(SLASH))
            if prompt is None:
                note(f"Unknown command {cmd}. Type /help.", WARN)
                return True
            console.print()
            message, images = self.expand_mentions(prompt)
            self.run_turn(message, shown=line.strip(), images=images)
            console.print()
        return True

    def _reload_llm(self) -> None:
        try:
            self.wait_idle()
            self.session.set_llm(make_llm(self.cfg), self.cfg.get("provider", ""), self.cfg.get("model", ""),
                                 context_tokens(self.cfg))
            console.print(Text.assemble(("  model: ", MUTED), (model_label(self.cfg), "bold")))
        except Exception as exc:  # noqa: BLE001
            self.show_error(exc)

    def shell(self, command: str) -> None:
        """`!command`: typed by the user, so it runs without an approval prompt."""
        command = command.strip()
        if not command:
            return
        moved = re.fullmatch(r"cd(\s+(?P<to>.+))?", command)
        if moved:  # a child shell's `cd` would be lost; move Vesper itself
            self.cd(moved.group("to") or "~")
            return
        try:
            proc = subprocess.run(command, shell=True, cwd=self.root)
            if proc.returncode:
                note(f"exit code {proc.returncode}", WARN)
        except KeyboardInterrupt:
            note("stopped", WARN)

    # ----- main loop -----
    def loop(self) -> None:
        prompt = self.make_prompt()
        self.mirror = TelegramMirror(prompt, enabled=self.cfg.get("telegram_mirror", True))
        self.mirror.start()
        try:
            self._loop(prompt)
        finally:
            self.mirror.close()

    def _loop(self, prompt) -> None:
        last_interrupt = 0.0
        while True:
            self.mirror.flush()  # what the bot did while this window was busy
            try:
                line = prompt.prompt(default=self._prefill)
                self._prefill = ""
            except KeyboardInterrupt:
                self._prefill = ""
                if time.monotonic() - last_interrupt < 2:
                    break
                last_interrupt = time.monotonic()
                note("Press Ctrl+C again to quit.")
                continue
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            console.print()
            console.print(user_bubble(line))
            word = line.split()[0].lower()
            # "/Users/me/notes.txt what is this?" is a message about a path, not a command.
            if line.startswith("/") and (word in SLASH or word in ALIASES or not Path(line.split()[0]).exists()):
                if not self.command(line):
                    break
                continue
            if line.startswith("!"):
                self.shell(line[1:])
                continue
            if re.match(r"vesper(\s|$)", line, re.I):  # a terminal command typed inside Vesper
                self.vesper_command(line)
                continue
            console.print()
            message, images = self.expand_mentions(line)
            self.run_turn(message, shown=line, images=images)
            console.print()
            if self.mode == "plan" and self._finished and self.offer_plan():
                console.print()
                self.run_turn("The plan is approved. Carry it out now.")
                console.print()
        if self.session is not None and self.session.history and self.store is not None:
            note(f"Saved. Continue later with: vesper -r {self.session.id[:8]}")
        console.print(Text("  ") + gradient("See you") + Text(" ✦", style=f"bold {ACCENT_3}"))


class TelegramMirror:
    """Shows what the Telegram bot does in this window while Vesper waits at the prompt:
    a background thread follows the bot's feed and prints above the prompt through
    prompt_toolkit's `run_in_terminal`, so the line being typed is kept. While this window
    is busy with its own turn, events wait and show before the next prompt."""

    def __init__(self, prompt, enabled: bool = True) -> None:
        from vesper import telegram as tg

        self.prompt = prompt
        self.enabled = enabled
        self.reader = tg.FeedReader()
        self.printer = FeedPrinter()
        self.queue: list[dict] = []
        self.lock = threading.Lock()
        self.pending = False
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._follow, daemon=True, name="vesper-telegram-mirror")

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(2)

    def _follow(self) -> None:
        while not self.stopped.wait(0.4):
            try:
                events = self.reader.read()
            except Exception:  # noqa: BLE001 - never take the terminal down for the mirror
                continue
            if not self.enabled:
                continue
            with self.lock:
                self.queue.extend(events)
                wanted = bool(self.queue) and not self.pending
            if wanted:
                self._kick()

    def _kick(self) -> None:
        app = getattr(self.prompt, "app", None)
        loop = getattr(app, "loop", None)
        if app is None or loop is None or not app.is_running:
            return  # the next prompt prints them
        with self.lock:
            self.pending = True
        try:
            loop.call_soon_threadsafe(self._print_above_prompt)
        except RuntimeError:  # the loop closed in between
            with self.lock:
                self.pending = False

    def _print_above_prompt(self) -> None:  # on the prompt's own loop
        from prompt_toolkit.application import run_in_terminal

        with self.lock:
            self.pending = False
        if self.prompt.app.is_running:
            run_in_terminal(self.flush)

    def flush(self) -> None:
        with self.lock:
            events, self.queue = self.queue, []
        for event in events:
            for part in self.printer.render(event):
                console.print(part)


# ---------------------------------------------------------------- one-shot mode
def attach_mentions(workspace, text: str, out_console) -> tuple[str, list[dict]]:
    """The files, folders and images a message names with `@path` (or as a dropped image path), attached.
    Returns (message text, image blocks) and prints what was attached to `out_console`."""
    from vesper import attachments as att

    refs = mention_refs(text)
    for path in dropped_paths(text):  # dragging a picture onto the terminal pastes its path
        if Path(path).suffix.lower() in att.IMAGE_TYPES and path not in refs:
            refs.append(path)
    refs = [r for r in refs if r.startswith(("/", "~")) or workspace.resolve(r).exists()]
    if not refs:
        return text, []
    out = att.attach(workspace, refs)
    for name in out.names:
        out_console.print(Text.assemble(("  ⎿  attached ", MUTED), (name, ACCENT)))
    for problem in out.problems:
        out_console.print(Text(f"  ⎿  not attached: {problem}", style=WARN))
    return out.message(text), out.images


def print_mode(root: Path, question: str, cfg: dict, mode: str, store=None, resume: dict | None = None,
               json_out: bool = False) -> int:
    """`vesper "question"`: the answer on stdout, tool activity on stderr, no prompts.

    Nobody can answer an approval prompt here, so in "ask" mode edits and commands are refused.
    With `json_out` (`--json`), stdout gets one JSON object per event instead, for scripts and CI:
    {"type": "token" | "tool_start" | "tool_end" | ..., ...} and a last {"type": "result", ...}.
    """
    def emit(kind: str, data: dict) -> None:
        print(json.dumps({"type": kind, **data}, ensure_ascii=False, default=str), flush=True)

    from vesper import providers as prov
    from vesper.coder import Session
    from vesper.workspace import Workspace

    if not prov.fake_llm_enabled() and (not cfg.get("provider") or not provider_ready(cfg["provider"])):
        err_console.print("No model is set up yet. Run `vesper` once to choose one.", style=ERR)
        return 2
    from vesper import permissions

    ws = Workspace(root, mode=mode, rules=permissions.load_rules("allow_commands"),
                   hosts=permissions.load_rules("allow_hosts"))

    def on_sigint(signum, frame):  # stop a running command first, or it would outlive us
        ws.cancel()
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, on_sigint)
    answer = []
    try:
        session = Session(make_llm(cfg), ws, store, session_id=resume["id"] if resume else None, source="print",
                          provider=cfg.get("provider", ""), model=cfg.get("model", ""),
                          context_tokens=context_tokens(cfg))
        message, images = attach_mentions(ws, question, err_console)
        shown = question if message != question else None
        usage: dict = {}
        for event, data in session.ask(message, shown=shown, images=images or None):
            if json_out:
                if event == "usage":
                    usage = data
                    data = {k: v for k, v in data.items() if k != "context_report"}  # aggregate sizes only
                if event != "done":
                    emit(event, data)
            if event == "token":
                answer.append(data["text"])
            elif event == "tool_start":
                if answer and not answer[-1].endswith("\n"):
                    answer.append("\n\n")  # text before and after a tool call are separate paragraphs
                if not json_out:
                    err_console.print(tool_start_line(data["name"], data["args"]).plain, style=MUTED)
            elif json_out:
                continue
            elif event == "tool_end" and not data["ok"]:
                err_console.print(tool_end_line(data["name"], data["output"], False, data.get("ms")).plain, style=MUTED)
            elif event == "notice":
                err_console.print(data["text"], style=WARN)
    except KeyboardInterrupt:
        if json_out:
            emit("result", {"ok": False, "error": "stopped", "text": "".join(answer).strip()})
        else:
            err_console.print("Stopped.", style=WARN)
        return 130
    except Exception as exc:  # noqa: BLE001
        if json_out:
            emit("result", {"ok": False, "error": friendly(exc, cfg), "text": "".join(answer).strip()})
        else:
            err_console.print(friendly(exc, cfg), style=ERR)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)
    if json_out:
        emit("result", {"ok": True, "text": "".join(answer).strip(), "session": session.id,
                        "usage": {k: usage.get(k) for k in ("input", "output", "cached", "cost", "seconds", "tools")}})
        return 0
    print("".join(answer).strip())
    return 0


# ---------------------------------------------------------------- `vesper sessions` and `vesper logs`
def sessions_command(argv: list[str]) -> int:
    if argv and argv[0] == "export":
        return sessions_export(argv[1:])
    if argv and argv[0] in ("rm", "delete", "rename"):
        return sessions_change(argv)
    ap = argparse.ArgumentParser(prog="vesper sessions", description="List saved conversations, newest first.",
                                 epilog="Also: vesper sessions rm ID [ID...] · vesper sessions rename ID TITLE · "
                                        "vesper sessions export [ID] [--html] [-o PATH]")
    ap.add_argument("-n", type=positive, default=20, help="how many to show (default 20)")
    ap.add_argument("--here", action="store_true", help="only conversations in the current folder")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)
    store = open_store()
    if store is None:
        return 1
    rows = store.list_sessions(args.n, Path.cwd().resolve() if args.here else None)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        note("No saved conversations yet. Start one with: vesper")
    else:
        console.print(sessions_table(rows))
        note("vesper -r ID resumes one · vesper logs ID shows what the agent did · vesper sessions rm ID deletes one")
    return 0


def positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def sessions_export(argv: list[str]) -> int:
    """vesper sessions export [ID] [--html] [-o PATH] [--thinking]"""
    from vesper import export

    ap = argparse.ArgumentParser(prog="vesper sessions export", description="Save a conversation as a Markdown or HTML file.")
    ap.add_argument("id", nargs="?", help="session id or its first characters (default: the latest conversation)")
    ap.add_argument("--html", action="store_true", help="a self-contained HTML page instead of Markdown")
    ap.add_argument("-o", "--output", help="file or folder to write (default: the current folder); - prints it")
    ap.add_argument("--thinking", action="store_true", help="include the model's reasoning")
    args = ap.parse_args(argv)
    store = open_store()
    if store is None:
        return 1
    if args.id:
        row = store.get_session(args.id)
    else:
        latest = store.list_sessions(1)
        row = latest[0] if latest else None
    if row is None:
        err_console.print(f"No conversation matches {args.id!r}." if args.id else "No saved conversations yet.", style=ERR)
        return 1
    fmt = "html" if args.html or (args.output or "").lower().endswith((".html", ".htm")) else "md"
    messages = store.load_messages(row["id"])
    if args.output == "-":
        sys.stdout.write(export.render(row, messages, fmt, args.thinking))
        return 0
    try:
        path = export.write(row, messages, fmt, Path(args.output) if args.output else None, args.thinking)
    except (OSError, ValueError) as exc:
        err_console.print(f"Could not export: {exc}", style=ERR)
        return 1
    note(f"Saved {path}")
    return 0


def sessions_change(argv: list[str]) -> int:
    """vesper sessions rm ID [ID...] | vesper sessions rename ID TITLE"""
    store = open_store()
    if store is None:
        return 1
    action, rest = argv[0], argv[1:]
    if action == "rename":
        if len(rest) < 2:
            err_console.print("Usage: vesper sessions rename ID NEW TITLE", style=ERR)
            return 2
        row = store.get_session(rest[0])
        if row is None:
            err_console.print(f"No conversation matches {rest[0]!r}.", style=ERR)
            return 1
        store.update_session(row["id"], title=" ".join(rest[1:]))
        note(f"Renamed {row['id']}.")
        return 0
    if not rest:
        err_console.print("Usage: vesper sessions rm ID [ID...]  (vesper sessions lists the ids)", style=ERR)
        return 2
    rows = [store.get_session(ref) for ref in rest]
    missing = [ref for ref, row in zip(rest, rows) if row is None]
    if missing:
        err_console.print(f"No conversation matches {', '.join(map(repr, missing))}. Nothing was deleted.", style=ERR)
        return 1
    for row in rows:
        store.delete_session(row["id"])
        note(f"Deleted {row['id']} {row['title']!r}.")
    return 0


def logs_command(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vesper logs", description="Show what the agent did in a conversation.")
    ap.add_argument("id", nargs="?", help="session id or its first characters (default: the latest conversation)")
    ap.add_argument("-n", type=positive, default=200, help="how many events to show (default 200)")
    ap.add_argument("--full", action="store_true", help="include tool arguments and output")
    ap.add_argument("--json", action="store_true", help="print one JSON object per line")
    args = ap.parse_args(argv)
    store = open_store()
    if store is None:
        return 1
    row = store.get_session(args.id) if args.id else store.latest()
    if row is None:
        err_console.print(f"No conversation matches {args.id!r}." if args.id else "No saved conversations yet.", style=ERR)
        return 1
    events = store.events(row["id"], args.n)
    if args.json:
        for e in events:
            print(json.dumps(e, ensure_ascii=False))
        return 0
    console.print(Text.assemble(("  " + (row["title"] or "(untitled)"), "bold"),
                                (f"  · {row['id']} · {tail_path(row['cwd'], 40)} · {row['model'] or 'model unknown'}", MUTED)))
    print_events(events, full=args.full)
    return 0


# ---------------------------------------------------------------- entry point
def read_piped(have_question: bool) -> str:
    """Text piped into `vesper "question"`, or "" when nothing is piped.

    A program that starts vesper with an open but silent stdin (a script, a CI step, another
    agent) would make a plain read() wait forever. So when the question is already on the
    command line, wait only STDIN_WAIT_S seconds for the first byte.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    if have_question and os.name == "posix":
        import select

        try:
            ready, _, _ = select.select([sys.stdin], [], [], STDIN_WAIT_S)
        except (OSError, ValueError):  # stdin is not a real file (a test, an embedded interpreter)
            ready = [sys.stdin]
        if not ready:
            return ""
    return sys.stdin.read(MAX_PIPED_CHARS + 1).strip()


def pick_resume(store, ref: str | None, here: Path, interactive: bool) -> dict | None:
    """The session for `-c` (ref is None: the latest one in this folder) or `-r [ID]`."""
    if store is None:
        return None
    if ref is None:
        return store.latest(here)
    if ref:
        return store.get_session(ref)
    rows = store.list_sessions(15)
    if not rows or not interactive:
        return rows[0] if rows else None
    console.print(sessions_table(rows))
    try:
        raw = input("  number or id: ").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    return rows[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(rows) else store.get_session(raw)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if argv and argv[0] == "app":
        open_app()
        return 0
    if argv and argv[0] == "sessions":
        return sessions_command(argv[1:])
    if argv and argv[0] == "logs":
        return logs_command(argv[1:])
    if argv and argv[0] == "acp":
        from vesper import acp

        return acp.main()
    if argv and argv[0] == "telegram":
        from vesper.telegram import command as telegram_command

        return telegram_command(argv[1:])
    if len(argv) == 1 and not argv[0].startswith("-"):
        import difflib

        close = difflib.get_close_matches(argv[0].lower(), ["sessions", "logs", "app", "telegram", "acp"], n=1, cutoff=0.75)
        if close and close[0] != argv[0]:
            err_console.print(f"Did you mean `vesper {close[0]}`? To ask the agent a one-word question, "
                              f"add a question mark: vesper \"{argv[0]}?\"", style=WARN)
            return 2

    ap = argparse.ArgumentParser(
        prog="vesper", description="Vesper: an AI coding agent for your terminal.",
        epilog="More: vesper sessions · vesper logs [ID] · vesper app · vesper telegram",
    )
    ap.add_argument("prompt", nargs="*", help="ask once and print the answer instead of starting a session")
    ap.add_argument("-C", "--dir", default=".", help="folder to start in (default: the current folder)")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="continue the last conversation in this folder")
    ap.add_argument("-r", "--resume", nargs="?", const="", metavar="ID",
                    help="resume a conversation by id (no id: pick from a list)")
    ap.add_argument("--mode", choices=list(MODES), help="ask (default), auto-edit, yolo or plan")
    ap.add_argument("--model", help="provider/model, e.g. openai/gpt-4.1-mini or ollama/llama3.2")
    ap.add_argument("--json", action="store_true", help="with a prompt: print one JSON object per event (for scripts and CI)")
    ap.add_argument("-v", "--version", action="version", version=f"vesper {__version__}")
    args = ap.parse_args(argv)

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        err_console.print(f"{root} is not a folder.", style=ERR)
        return 2
    cfg = load_config()
    apply_theme(cfg.get("theme"))
    if args.model:
        from vesper import providers as prov

        pid, _, model = args.model.partition("/")
        if pid not in prov.PROVIDERS:
            err_console.print(f"Unknown provider {pid!r}. Choose one of: {', '.join(prov.PROVIDERS)}", style=ERR)
            return 2
        cfg.update(provider=pid, model=model)
    mode = args.mode or (cfg.get("agent_mode") if cfg.get("agent_mode") in MODES and cfg["agent_mode"] != "plan" else "ask")

    piped = read_piped(bool(args.prompt))
    if len(piped) > MAX_PIPED_CHARS:
        piped = piped[:MAX_PIPED_CHARS] + "\n[... input cut off]"
    one_shot = bool(args.prompt or piped)

    store = open_store()
    resume = None
    if args.cont or args.resume is not None:
        resume = pick_resume(store, None if args.cont else args.resume, root, interactive=not one_shot)
        if resume is None:
            if args.resume:
                err_console.print(f"No conversation matches {args.resume!r}. See: vesper sessions", style=ERR)
                return 2
            err_console.print("No earlier conversation to continue; starting a new one.", style=MUTED)
        elif Path(resume["cwd"]).is_dir():
            root = Path(resume["cwd"])

    if one_shot:
        question = " ".join(args.prompt)
        if piped:  # `git diff | vesper "write a commit message"`: the piped text comes with the question
            question = f"{question}\n\n<stdin>\n{piped}\n</stdin>" if question else piped
        return print_mode(root, question, cfg, mode, store, resume, json_out=args.json)

    console.print()
    if not trust_folder(root):
        return 1
    if not first_run(cfg):
        note("No model chosen. Run vesper again when you are ready.")
        return 1
    app = App(root, mode, cfg, store)
    banner(root, cfg, mode)
    with console.status(Text("Starting…", style=MUTED), spinner="vesper", spinner_style=f"bold {ACCENT_3}"):
        try:
            app.build(resume)
        except Exception as exc:  # noqa: BLE001
            app.show_error(exc)
            return 1
    if resume:
        recap(app.session, resume)
    else:
        app.show_recent()
    app.loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
