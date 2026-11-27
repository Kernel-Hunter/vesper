"""How the terminal app looks: colors, the banner, tool lines, the streaming answer,
and the tables for sessions and the activity log.

Nothing here talks to a model or touches files; `tui.py` decides what to show and when.
The colors come from a theme (`themes.py`); `apply_theme` switches them everywhere at once.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import sys
import time
from pathlib import Path

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.markdown import CodeBlock, Heading, Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import SPINNERS, Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from vesper import __version__, themes

# The colors in use. `apply_theme` rebinds these names here and in the modules that
# imported them (tui, onboarding), so every screen follows a theme change at once.
THEME: themes.Palette = themes.THEMES[themes.DEFAULT]
ACCENT = THEME.accent
ACCENT_2 = THEME.accent_2
ACCENT_3 = THEME.accent_3
TEXT = THEME.text
MUTED = THEME.muted
FAINT = THEME.faint
OK = THEME.ok
ERR = THEME.err
WARN = THEME.warn

MODES = {
    "ask": "asks before edits and commands",
    "auto-edit": "edits files freely, asks before commands",
    "yolo": "never asks (careful)",
    "plan": "reads and plans; changes nothing until you approve the plan",
}
TOOL_LABELS = {
    "list_files": "List", "read_file": "Read", "glob": "Find", "grep": "Search",
    "change_directory": "Go to", "write_file": "Write",
    "edit_file": "Edit", "run_command": "Run", "update_tasks": "Tasks", "web_fetch": "Fetch",
    "web_search": "Search the web", "run_check": "Check",
}
# The argument that says most about a tool call.
MAIN_ARG = {
    "read_file": "path", "write_file": "path", "edit_file": "path", "list_files": "path",
    "change_directory": "path", "glob": "pattern", "grep": "pattern",
    "run_command": "command", "run_check": "command", "web_fetch": "url", "web_search": "query",
}
READING = frozenset({"list_files", "read_file", "glob", "grep", "change_directory", "web_fetch", "web_search"})
CHANGING = frozenset({"write_file", "edit_file"})
LIVE_OUTPUT_LINES = 5     # lines of a running command kept on screen
LIVE_THOUGHT_LINES = 4    # lines of the model's thinking kept on screen while it thinks
VERBOSE_OUTPUT_LINES = 60  # lines of tool output shown in verbose mode
AUTO_DIFF_LINES = 24      # lines of a diff shown for an edit that did not need approval

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

# A star that twinkles while Vesper works.
SPINNERS["vesper"] = {"interval": 110, "frames": ["✦", "✧", "⋆", "·", "⋆", "✧"]}


def _markdown_theme() -> Theme:
    return Theme({
        "markdown.code": f"bold {ACCENT_2}", "markdown.link": ACCENT, "markdown.link_url": f"underline {ACCENT}",
        "markdown.h1": f"bold {ACCENT}", "markdown.h2": f"bold {TEXT}", "markdown.h3": f"bold {MUTED}",
        "markdown.h4": f"italic {MUTED}", "markdown.item.bullet": ACCENT, "markdown.item.number": ACCENT,
        "markdown.block_quote": f"italic {MUTED}", "markdown.hr": FAINT, "markdown.table.border": FAINT,
        "markdown.table.header": f"bold {TEXT}",
    })


_themed: set[int] = set()  # consoles that already carry a pushed Markdown theme


def apply_theme(name: str | None = None) -> themes.Palette:
    """Use the theme called `name` (the default for this terminal when unknown or None)."""
    global THEME
    THEME = themes.resolve(name)
    colors = {"ACCENT": THEME.accent, "ACCENT_2": THEME.accent_2, "ACCENT_3": THEME.accent_3, "TEXT": THEME.text,
              "MUTED": THEME.muted, "FAINT": THEME.faint, "OK": THEME.ok, "ERR": THEME.err, "WARN": THEME.warn}
    globals().update(colors)
    for module in ("vesper.tui", "vesper.onboarding"):
        mod = sys.modules.get(module)
        if mod is not None:
            mod.__dict__.update({k: v for k, v in colors.items() if k in mod.__dict__})
    for con in (console, err_console):
        if id(con) in _themed:
            con.pop_theme()
        con.push_theme(_markdown_theme())
        _themed.add(id(con))
    return THEME


# ---------------------------------------------------------------- small pieces
def _rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def blend(a: str, b: str, t: float) -> str:
    """The color a fraction `t` of the way from `a` to `b`."""
    x, y = _rgb(a), _rgb(b)
    c = [round(x[j] + (y[j] - x[j]) * t) for j in range(3)]
    return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"


def ramp(stops: list[str], t: float) -> str:
    """The color at `t` (0 to 1) along a line through several colors."""
    if len(stops) == 1:
        return stops[0]
    t = min(1.0, max(0.0, t)) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    return blend(stops[i], stops[i + 1], t - i)


def gradient(text: str, start: str | None = None, end: str | None = None, bold: bool = True) -> Text:
    stops = [start or ACCENT, end or ACCENT_2] if start or end else [ACCENT, ACCENT_2, ACCENT_3]
    out = Text()
    n = max(1, len(text) - 1)
    for i, ch in enumerate(text):
        out.append(ch, style=("bold " if bold else "") + ramp(stops, i / n))
    return out


def short_path(p: Path | str) -> str:
    p = Path(p)
    home = Path.home()
    if p == home:
        return "~"
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return str(p)


def tail_path(p: Path | str, width: int = 40) -> str:
    """short_path(), keeping the end when it is too long: the last folders say the most."""
    text = short_path(p)
    if len(text) <= width:
        return text
    parts = Path(text).parts
    kept: list[str] = []
    for part in reversed(parts):
        if len("/".join([part, *kept])) + 2 > width and kept:
            break
        kept.insert(0, part)
    return "…/" + "/".join(kept)


def model_label(cfg: dict) -> str:
    from vesper import providers as prov

    p = prov.PROVIDERS.get(cfg.get("provider", ""))
    if p is None:
        return "no model"
    return f"{p.label} · {cfg.get('model') or p.chat_model}"


def fmt_tokens(n: int) -> str:
    for size, suffix in ((10**12, "T"), (10**9, "B"), (10**6, "M"), (1000, "K")):
        if n >= size:
            value = n / size
            return f"{value:.2f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def fmt_ago(timestamp: float) -> str:
    gap = max(0, time.time() - timestamp)
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600), (86400 * 14, "d", 86400)):
        if gap < limit:
            return f"{int(gap // size)}{unit} ago"
    return dt.datetime.fromtimestamp(timestamp).strftime("%d %b %Y")


def meter(fraction: float, width: int = 10) -> str:
    """A bar `width` cells wide, filled to `fraction` with eighth-cell precision."""
    eighths = round(min(1.0, max(0.0, fraction)) * width * 8)
    full, part = divmod(eighths, 8)
    bar = "█" * full + ("▏▎▍▌▋▊▉"[part - 1] if part else "")
    return bar + " " * (width - len(bar))


def meter_parts(fraction: float, width: int = 10) -> list[tuple[str, str]]:
    """The meter as (text, color) pieces: the filled part, then a faint track."""
    bar = meter(fraction, width).rstrip(" ")
    return [(bar, meter_color(fraction)), ("─" * (width - len(bar)), FAINT)]


def meter_color(fraction: float) -> str:
    return OK if fraction < 0.6 else WARN if fraction < 0.85 else ERR


_branch_cache: dict[str, tuple[float, str]] = {}


def git_branch(folder: Path | str) -> str:
    """The checked-out git branch of `folder` (empty when it is not in a repository)."""
    key = str(folder)
    hit = _branch_cache.get(key)
    if hit and time.monotonic() - hit[0] < 2:
        return hit[1]
    branch = ""
    p = Path(folder)
    for d in (p, *p.parents):
        git = d / ".git"
        if not git.exists():
            continue
        try:
            text = (git / "HEAD").read_text(errors="replace").strip() if git.is_dir() else ""
        except OSError:
            text = ""
        if text.startswith("ref:"):
            branch = text.removeprefix("ref: refs/heads/")
        elif text:
            branch = text[:7]  # a detached head: the commit
        break
    _branch_cache[key] = (time.monotonic(), branch)
    return branch


# ---------------------------------------------------------------- the banner
# Letters on a grid seven dots tall with two-dot strokes. Each line of text shows two dot rows:
# a half block whose foreground paints the top dot and whose background paints the bottom one.
_GLYPHS = {
    "V": ["XX...XX", "XX...XX", "XX...XX", ".XX.XX.", ".XX.XX.", "..XXX..", "...X..."],
    "E": ["XXXXXX", "XX....", "XX....", "XXXXX.", "XX....", "XX....", "XXXXXX"],
    "S": [".XXXXX", "XX....", "XX....", ".XXXX.", "....XX", "....XX", "XXXXX."],
    "P": ["XXXXX.", "XX..XX", "XX..XX", "XXXXX.", "XX....", "XX....", "XX...."],
    "R": ["XXXXX.", "XX..XX", "XX..XX", "XXXXX.", "XX.XX.", "XX..XX", "XX..XX"],
}


def _toward_background(color: str, amount: float) -> str:
    return blend(color, "#000000" if THEME.dark else "#ffffff", amount)


def wordmark(word: str = "VESPER", gap: int = 2) -> list[Text]:
    """VESPER in solid letters. The color runs from dusk blue to the evening star's gold,
    and each letter is a little brighter at the top, as if lit from above."""
    rows = [""] * 7
    for n, ch in enumerate(word):
        for r in range(7):
            rows[r] += ("." * gap if n else "") + _GLYPHS[ch][r]
    width, height = len(rows[0]), 8  # the eighth dot row stays empty
    stops = [ACCENT, ACCENT_2, ACCENT_3]

    def dot(x: int, y: int) -> str | None:
        base = ramp(stops, x / max(1, width - 1))
        if y < 7 and x < width and rows[y][x] == "X":
            return blend(base, "#ffffff", 0.25 * (1 - y / 6)) if THEME.dark else _toward_background(base, 0.1 * y / 6)
        return None

    lines = []
    for r in range(height // 2):
        line = Text()
        for x in range(width):
            top, bottom = dot(x, 2 * r), dot(x, 2 * r + 1)
            if top is None and bottom is None:
                line.append(" ")
            elif top == bottom:
                line.append("█", style=top)
            elif top is None:
                line.append("▄", style=bottom)
            elif bottom is None:
                line.append("▀", style=top)
            else:
                line.append("▀", style=f"{top} on {bottom}")
        lines.append(line)
    return lines


# A few stars beside the name: (column, line, glyph, how bright 0 to 1). The first is the evening star.
_SKY = [(4, 0, "✦", 1.0), (11, 1, "·", 0.35), (17, 0, "⋆", 0.5), (8, 3, "·", 0.3), (21, 2, "·", 0.25),
        (15, 3, "✧", 0.45), (1, 2, "·", 0.2)]


def sky(width: int = 24, height: int = 4) -> list[Text]:
    grid = [[" "] * width for _ in range(height)]
    styles: dict[tuple[int, int], str] = {}
    for x, y, glyph, bright in _SKY:
        if x < width and y < height:
            grid[y][x] = glyph
            color = ACCENT_3 if bright >= 1 else blend(FAINT, TEXT, bright)
            styles[(x, y)] = ("bold " if bright >= 1 else "") + color
    out = []
    for y in range(height):
        line = Text()
        for x in range(width):
            line.append(grid[y][x], style=styles.get((x, y), ""))
        out.append(line)
    return out


def horizon(width: int) -> Text:
    """A thin line that fades out to the right, like the last light at dusk."""
    out = Text()
    for x in range(width):
        out.append("─", style=blend(ACCENT_3, FAINT, min(1.0, x / max(1, width * 0.7))))
    return out


def mode_color(mode: str) -> str:
    return {"ask": OK, "auto-edit": WARN, "yolo": ERR, "plan": ACCENT_2}.get(mode, MUTED)


def tint(amount: float = 0.12) -> str:
    """A background a little lighter (or darker, on light themes) than the terminal's, tinted
    with the accent: for message bubbles and keycaps."""
    base = blend(FAINT, "#000000", 0.55) if THEME.dark else blend(ACCENT, "#ffffff", 0.9)
    return blend(base, ACCENT, amount)


def keycap(key: str, color: str | None = None) -> Text:
    """A key drawn like a key: ` y ` on a raised background."""
    out = Text()
    out.append(f" {key} ", style=f"bold {color or TEXT} on {tint(0.18)}")
    return out


def keys_line(items: list[tuple[str, str]], indent: int = 2, color: str | None = None) -> Text:
    """Keycaps with what they do: [/] commands   [@] attach ..."""
    out = Text(" " * indent)
    for n, (key, what) in enumerate(items):
        if n:
            out.append("   ")
        out += keycap(key, color or ACCENT)
        out.append(f" {what}", style=MUTED)
    return out


def banner(root: Path, cfg: dict, mode: str) -> None:
    pad = "  "
    if console.width < 64:  # narrow: the name on one line
        console.print(Text.assemble(pad, ("✦ ", f"bold {ACCENT_3}"), gradient("Vesper"), (f"  v{__version__}", MUTED)))
        console.print(Text(pad + tail_path(root, console.width - 4), style=MUTED))
        return
    mark = wordmark()
    stars = sky(min(24, console.width - mark[0].cell_len - 6), len(mark))
    console.print()
    for line, star_line in zip(mark, stars):
        console.print(Text(pad) + line + Text("   ") + star_line)
    width = mark[0].cell_len
    console.print(Text.assemble(pad, (f"v{__version__}", FAINT), ("  ·  ", FAINT), ("a coding agent", MUTED)))
    console.print(Text(pad) + horizon(min(width + 14, console.width - 4)))
    branch = git_branch(root)
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style=FAINT, min_width=6)
    rows.add_column()
    rows.add_row("model", Text(model_label(cfg), style=TEXT))
    rows.add_row("folder", Text.assemble((tail_path(root, 60), TEXT), (f"  ⎇ {branch}", MUTED) if branch else ""))
    rows.add_row("mode", Text.assemble((mode, f"bold {mode_color(mode)}"), (f"  {MODES[mode]}", MUTED)))
    console.print(Padding(rows, (0, 0, 0, 2)))
    console.print()
    console.print(keys_line([("/", "commands"), ("ctrl+r", "history"), ("@", "attach"), ("!", "shell"),
                             ("⇧ tab", "mode"), ("/theme", "look")]))
    console.print()
    if themes.swapped_from is not None:
        kind = "light" if not themes.swapped_from.dark else "dark"
        note(f"Your theme, {themes.swapped_from.label}, is made for {kind} terminals and this one is not, "
             f"so Vesper uses {THEME.label} here. /theme picks another.")
        console.print()


def user_bubble(text: str) -> Padding:
    """What you sent, on a tinted band the width of the window."""
    body = Text.assemble(("❯ ", f"bold {ACCENT}"), (text, f"{TEXT}"))
    return Padding(body, (0, 1), style=f"on {tint()}", expand=True)


def theme_swatch(p: themes.Palette, current: bool = False) -> Text:
    """One line of /theme: the name in its own colors and its mood."""
    stops = [p.accent, p.accent_2, p.accent_3]
    out = Text("  ● " if current else "    ", style=f"bold {p.accent}")
    word = p.label.ljust(10)
    for i, ch in enumerate(word):
        out.append(ch, style="bold " + ramp(stops, i / max(1, len(word) - 1)))
    for i in range(14):
        out.append("▆", style=ramp(stops, i / 13))
    name = p.name.removesuffix("-dark") if p.name in themes.VARIANTS else p.name  # /theme daylight fits any window
    out.append(f"  {name:<9}", style=MUTED)
    out.append(p.mood, style=FAINT)
    return out


def error_panel(message: str, hint: str = "") -> None:
    body = Text(message, style=TEXT)
    if hint:
        body.append("\n" + hint, style=MUTED)
    console.print(Panel(body, title=Text(" ✗ error ", style=f"bold {ERR}"), title_align="left", border_style=ERR,
                        box=box.ROUNDED, expand=False, padding=(0, 1)))


def note(text: str, style: str | None = None) -> None:
    console.print(Text("  " + text, style=style or MUTED))


# ---------------------------------------------------------------- tool calls
def summarize_args(name: str, args: dict) -> str:
    key = MAIN_ARG.get(name)
    value = str(args.get(key, "")) if key else ""
    if name in ("grep", "glob") and args.get("path") not in (None, "", "."):
        value += f"  in {args['path']}"
    value = value.replace("\n", " ")
    return value if len(value) <= 90 else value[:87] + "…"


def summarize_output(name: str, output: str, ok: bool, limit: int = 6) -> Text:
    """A tool result in a few words. `limit` is how many lines of command output to keep."""
    if not ok:
        first = output.removeprefix("Error:").strip().splitlines()[0] if output.strip() else "failed"
        return Text(first[:200], style=ERR)
    lines = output.splitlines()
    if name == "read_file":
        pages = sum(1 for line in lines if line.startswith("[page "))
        if pages:
            return Text(f"{pages} page{'s' if pages != 1 else ''}", style=MUTED)
        n = sum(1 for line in lines if re.match(r"^\s*\d+\t", line))
        return Text(f"{n} line{'s' if n != 1 else ''}" if n else (lines[0][:120] if lines else ""), style=MUTED)
    if name in ("glob", "grep"):
        n = 0 if output.startswith("No ") else len([line for line in lines if not line.startswith(("...", "("))])
        return Text(f"{n} result{'s' if n != 1 else ''}", style=MUTED)
    if name == "list_files":
        n = max(0, len(lines) - 1)
        return Text(f"{n} entr{'y' if n == 1 else 'ies'}", style=MUTED)
    if name == "change_directory":
        return Text(short_path(lines[0].removeprefix("Working directory is now ")) if lines else "", style=MUTED)
    if name in ("run_command", "run_check"):
        head = lines[0] if lines else ""
        body = [line for line in lines[1:] if line.strip()]
        rest = body[:limit]
        t = Text(head, style=OK if head.startswith("exit code 0") else WARN)
        for line in rest:
            t.append("\n      " + line[:160], style=MUTED)
        if len(body) > len(rest):
            t.append(f"\n      +{len(body) - len(rest)} more lines (ctrl+o shows all)", style=FAINT)
        return t
    if name in ("write_file", "edit_file") and len(lines) > 1:  # language server errors follow
        t = Text(lines[0][:200], style=MUTED)
        for line in lines[1:12]:
            t.append("\n      " + line.strip()[:160], style=WARN)
        return t
    return Text(lines[0][:200] if lines else "done", style=MUTED)


def tool_color(name: str) -> str:
    return ACCENT_2 if name in READING else ACCENT_3 if name in CHANGING else ACCENT


def tool_start_line(name: str, args: dict) -> Text:
    return Text.assemble(("● ", tool_color(name)), (TOOL_LABELS.get(name, name), f"bold {TEXT}"),
                         ("  " + summarize_args(name, args), MUTED))


def tool_end_line(name: str, output: str, ok: bool, ms: int | None, verbose: bool = False) -> Text:
    line = Text("  ╰ ", style=FAINT)
    if not ok:
        line.append("✗ ", style=f"bold {ERR}")
    line += summarize_output(name, output, ok, VERBOSE_OUTPUT_LINES if verbose else 6)
    if ms is not None and ms >= 1000:
        line.append(f"  {fmt_duration(ms / 1000)}", style=FAINT)
    if verbose and ok and len(output.splitlines()) > 1 and name not in ("run_command", "write_file", "edit_file"):
        shown = output.splitlines()[:VERBOSE_OUTPUT_LINES]
        for row in shown:
            line.append("\n      " + row[:200], style=MUTED)
        extra = len(output.splitlines()) - len(shown)
        if extra > 0:
            line.append(f"\n      +{extra} more lines", style=FAINT)
    return line


class DiffView:
    """A unified diff with line numbers and tinted added and removed lines."""

    def __init__(self, diff: str, limit: int | None = None) -> None:
        self.diff, self.limit = diff, limit

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        rows = self.diff.splitlines()
        shown = rows[:self.limit] if self.limit else rows
        old = new = 0
        for row in shown:
            if row.startswith(("--- ", "+++ ")):
                continue
            hunk = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", row)
            if hunk:
                where = hunk.group(3).strip()
                if where or old or new:  # a function name, or a jump between hunks
                    yield Text(f"    ⋯ {where}".rstrip(), style=FAINT)
                old, new = int(hunk.group(1)), int(hunk.group(2))
                continue
            if row.startswith("+"):
                num, mark, color, bg = new, "+", OK, THEME.add_bg
                new += 1
            elif row.startswith("-"):
                num, mark, color, bg = old, "-", ERR, THEME.del_bg
                old += 1
            else:
                num, mark, color, bg = new, " ", MUTED, ""
                old, new = old + 1, new + 1
            body = row[1:] if row[:1] in ("+", "-", " ") else row
            line = Text.assemble((f"{num:>4} " if num else "     ", FAINT), (mark + " ", f"bold {color}"), (body, TEXT))
            line.truncate(width)
            line.pad_right(width - line.cell_len)
            if bg:
                line.stylize(f"on {bg}")
            yield line
        if self.limit and len(rows) > self.limit:
            yield Text(f"     +{len(rows) - self.limit} more lines", style=FAINT)


def diff_panel(action: dict, title: str, border: str | None = None, limit: int | None = None) -> Panel:
    diff = action.get("diff") or ""
    body = DiffView(diff, limit) if diff.strip() else Text("(no changes)", style=MUTED)
    return Panel(body, title=Text(f" {title} ", style=f"bold {TEXT}"), title_align="left",
                 border_style=border or WARN, box=box.ROUNDED, padding=(0, 1))


def turn_footer(usage: dict, context_tokens: int) -> Text:
    parts = [fmt_duration(usage["seconds"])]
    if usage["tools"]:
        parts.append(f"{usage['tools']} tool call{'s' if usage['tools'] != 1 else ''}")
    approx = "~" if usage.get("estimated") else ""
    cached = f" ({fmt_tokens(usage['cached'])} cached)" if usage.get("cached") else ""
    parts.append(f"{approx}{fmt_tokens(usage['input'])} in{cached}, {approx}{fmt_tokens(usage['output'])} out")
    if usage.get("cost") is not None:
        parts.append(fmt_cost(usage["cost"]) + (f" (chat {fmt_cost(usage['total_cost'])})" if usage.get("total_cost") else ""))
    out = Text.assemble(("  ◦ ", FAINT), (" · ".join(parts), FAINT))
    if context_tokens:
        used = usage["context"] / context_tokens
        if out.cell_len + 22 <= console.width:
            out.append("  ·  context ", style=FAINT)
            for piece, color in meter_parts(used, 6):
                out.append(piece, style=color)
            out.append(f" {round(100 * used)}%", style=FAINT)
        elif out.cell_len + 12 <= console.width:
            out.append(f" · ctx {round(100 * used)}%", style=FAINT)
    return out


def fmt_cost(dollars: float) -> str:
    """$0 for free, then enough digits to see small amounts: $0.0004, $0.012, $1.24."""
    if not dollars:
        return "$0"
    if dollars < 0.01:
        return f"${dollars:.4f}"
    return f"${dollars:.3f}" if dollars < 1 else f"${dollars:,.2f}"


def tasks_block(items: list[dict]) -> Text:
    """The agent's task list: done, in progress, to do."""
    out = Text()
    for n, item in enumerate(items):
        status = item.get("status")
        mark, style = {"done": ("✓", OK), "active": ("◆", f"bold {ACCENT}")}.get(status, ("◇", MUTED))
        out.append(("\n" if n else "") + f"  {mark} ", style=style)
        text_style = FAINT if status == "done" else f"bold {TEXT}" if status == "active" else TEXT
        out.append(str(item.get("text", "")), style=text_style)
    return out


# ---------------------------------------------------------------- Markdown
_BAR = box.Box("    \n▎   \n▎   \n▎   \n▎   \n▎   \n▎   \n    \n")


class _Heading(Heading):
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        if self.tag == "h1":
            yield Text("")
            yield text
            yield Text("─" * min(text.cell_len + 2, options.max_width), style=FAINT)
        else:
            if self.tag == "h2":
                yield Text("")
            yield text


class _Code(CodeBlock):
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        syntax = Syntax(code, self.lexer_name, theme="ansi_dark" if THEME.dark else "ansi_light",
                        word_wrap=True, background_color="default")
        lang = self.lexer_name if self.lexer_name not in ("", "text") else ""
        yield Panel(syntax, box=_BAR, border_style=FAINT, padding=(0, 1), expand=True,
                    title=Text(f"{lang} ", style=f"italic {FAINT}") if lang else None, title_align="left")


class VMarkdown(Markdown):
    """rich's Markdown with left-aligned headings and code blocks marked by a thin bar."""

    elements = {**Markdown.elements, "heading_open": _Heading, "fence": _Code, "code_block": _Code}


def answer(text: str, mark: bool = False):
    """The agent's words, two columns in, under the tool names. `mark` puts Vesper's star in
    the gutter: on the first block of each stretch of writing, like ❯ marks yours."""
    body = VMarkdown(text, code_theme="ansi_dark" if THEME.dark else "ansi_light")
    if not mark:
        return Padding(body, (0, 0, 0, 2))
    grid = Table.grid()
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text("✦", style=f"bold {ACCENT_3}"), body)
    return grid


# ---------------------------------------------------------------- streaming renderer
def _last_block_end(text: str) -> int:
    """Index just after the last blank line that is not inside a ``` fence, or 0."""
    end = 0
    inside = False
    pos = 0
    for line in text.splitlines(keepends=True):
        pos += len(line)
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if not inside and not line.strip() and pos < len(text):
            end = pos
    return end


def shimmer(text: str, since: float) -> Text:
    """`text` with a soft band of light moving across it, left to right."""
    out = Text()
    span = len(text) + 8
    pos = ((time.monotonic() - since) * 14) % span - 4
    for i, ch in enumerate(text):
        glow = max(0.0, 1 - abs(i - pos) / 3.5)
        out.append(ch, style=blend(MUTED, TEXT, math.sin(glow * math.pi / 2)))
    return out


class _Busy:
    """The spinner line: what is happening, for how long, and the tail of a running command."""

    def __init__(self) -> None:
        self.spinner = Spinner("vesper", style=f"bold {ACCENT_3}")
        self.status = "Thinking"
        self.since = time.monotonic()
        self.tail: list[str] = []

    def __rich__(self):
        waited = time.monotonic() - self.since
        text = shimmer(f"{self.status}…", self.since)
        if waited >= 2:
            text.append(f"  {fmt_duration(waited)}", style=FAINT)
        if waited >= 4:
            text.append("  ·  ctrl+c to stop", style=FAINT)
        self.spinner.update(text=text, style=f"bold {ACCENT_3}")
        if not self.tail:
            return self.spinner
        width = max(20, console.width - 8)
        return Group(self.spinner, *[Text("  │ " + line[:width], style=FAINT) for line in self.tail])


class Renderer:
    """Streams Markdown: finished blocks are printed for good, the open block stays live."""

    def __init__(self) -> None:
        self.buffer = ""
        self.live: Live | None = None
        self.busy = _Busy()
        self.thinking = ""                      # reasoning text of the step in progress
        self.thinking_since: float | None = None
        self.marked = False                     # the star is drawn for this stretch of writing

    def new_stretch(self) -> None:
        """A tool ran or a new turn began: the next words get the star again."""
        self.marked = False

    def _answer(self, text: str):
        out = answer(text, mark=not self.marked)
        return out

    def _renderable(self):
        return self._answer(self.buffer) if self.buffer.strip() else self.busy

    def start(self, status: str | None = None) -> None:
        if status and status != self.busy.status:
            self.busy.status, self.busy.since, self.busy.tail = status, time.monotonic(), []
        if self.live is None:
            self.live = Live(self._renderable(), console=console, refresh_per_second=12, transient=True,
                             vertical_overflow="crop")
            self.live.start()
        else:
            self.live.update(self._renderable())

    def restart(self, status: str) -> None:
        """Like start(), but the clock starts again even if the status text is the same."""
        self.busy.status, self.busy.since, self.busy.tail = status, time.monotonic(), []
        self.start()

    def output(self, line: str) -> None:
        """One more line from a running command."""
        self.busy.tail = [*self.busy.tail, line][-LIVE_OUTPUT_LINES:]

    def progress(self, status: str) -> None:
        """Change the spinner text without restarting its clock (a tool call being written)."""
        self.busy.status = status
        self.start()

    def thought(self, text: str) -> None:
        """One more piece of the model's thinking: its last lines scroll under the spinner."""
        if self.thinking_since is None:
            self.thinking_since = time.monotonic()
            self.busy.status = "Thinking"
        self.thinking += text
        width = max(20, console.width - 8)
        rows = [row[i:i + width] for row in self.thinking.splitlines() if row.strip() for i in range(0, len(row), width)]
        self.busy.tail = rows[-LIVE_THOUGHT_LINES:]
        self.start()

    def end_thought(self, verbose: bool) -> None:
        """The model stopped thinking: leave one line behind, or the whole text in transcript view."""
        text, began = self.thinking.strip(), self.thinking_since
        self.thinking, self.thinking_since, self.busy.tail = "", None, []
        if not text:
            return
        self.stop()
        took = fmt_duration(time.monotonic() - (began or time.monotonic()))
        if verbose:
            console.print(Text(f"∴ Thinking ({took})", style=f"italic {ACCENT}"))
            console.print(Padding(Text(text, style=f"italic {MUTED}"), (0, 0, 0, 2)))
        else:
            console.print(Text(f"∴ Thought for {took}", style=f"italic {FAINT}"))

    def token(self, text: str) -> None:
        self.buffer += text
        cut = _last_block_end(self.buffer)
        if cut > 0:
            done, self.buffer = self.buffer[:cut], self.buffer[cut:]
            if done.strip():
                console.print(self._answer(done))
                self.marked = True
        self.start()

    def flush(self) -> None:
        text, self.buffer = self.buffer, ""
        self.stop()
        if text.strip():
            console.print(self._answer(text))
            self.marked = True

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None


# ---------------------------------------------------------------- sessions and logs
def sessions_table(rows: list[dict], current: str = "") -> Table:
    t = Table(box=box.SIMPLE_HEAD, header_style=FAINT, border_style=FAINT, padding=(0, 1))
    room = max(16, min(48, console.width - 64))  # what is left for the title on a narrow terminal
    t.add_column("#", justify="right", style=ACCENT, min_width=2, no_wrap=True)
    t.add_column("id", style=MUTED, min_width=8, no_wrap=True)
    t.add_column("when", style=MUTED, min_width=8, no_wrap=True)
    t.add_column("title", overflow="ellipsis", no_wrap=True, max_width=room)
    t.add_column("folder", style=MUTED, overflow="ellipsis", no_wrap=True, max_width=24)
    t.add_column("msgs", justify="right", style=MUTED, min_width=4, no_wrap=True)
    for i, row in enumerate(rows, start=1):
        mark = " ◀" if row["id"] == current else ""
        t.add_row(str(i), row["id"][:8], fmt_ago(row["updated_at"]), (row["title"] or "(untitled)") + mark,
                  tail_path(row["cwd"], 24), str(row.get("messages", "")))
    return t


def replay(turns: list[dict], max_turns: int = 24) -> None:
    """A saved conversation drawn the way it looked: your messages, Vesper's words, each step."""
    shown = turns[-max_turns:]
    hidden = len(turns) - len(shown)
    if hidden:
        console.print(Text(f"  ⋯ {hidden} earlier messages · /transcript shows everything", style=FAINT))
    for turn in shown:
        console.print()
        if turn["role"] == "user":
            console.print(user_bubble(turn["text"]))
            continue
        marked = False
        for part in turn["parts"]:
            if part["type"] == "reasoning":
                continue
            if part["type"] == "text":
                if part["text"].strip():
                    console.print(answer(part["text"], mark=not marked))
                    marked = True
                continue
            console.print(tool_start_line(part["name"], part["args"]))
            if part["output"] is None:
                console.print(Text("  ╰ (stopped before it finished)", style=WARN))
            else:
                console.print(tool_end_line(part["name"], part["output"], bool(part["ok"]), None))
            marked = False


def recap(session, row: dict) -> None:
    """A resumed conversation: its title, then the conversation itself, then where it stops."""
    from vesper.coder import transcript

    console.print()
    console.print(Text.assemble(("  ↺ Resumed  ", f"bold {ACCENT}"), (row["title"] or "(untitled)", f"bold {TEXT}"),
                                (f"  ·  {len(session.history)} messages  ·  {fmt_ago(row['updated_at'])}", FAINT)))
    replay(transcript(session.history))
    console.print()
    width = min(console.width - 4, 72)
    label = " you are here "
    side = max(2, (width - len(label)) // 2)
    console.print(Text.assemble(("  " + "─" * side, FAINT), (label, f"italic {MUTED}"), ("─" * side, FAINT)))
    store = getattr(session, "store", None)
    if store is not None and session.history and store.cut_off(session.id):
        console.print(Text.assemble(("  ! ", WARN), ("The last answer was cut off when Vesper closed. Type ", MUTED),
                                    ("continue", "bold"), (" to pick up where it stopped.", MUTED)))


def recent_block(rows: list[dict]) -> None:
    """The last few conversations on the start screen, so going back is one key away."""
    if not rows:
        return
    console.print(Text("  Recent", style=f"bold {TEXT}"))
    for row in rows:
        title = (row["title"] or "(untitled)")
        title = title if len(title) <= 50 else title[:49] + "…"
        console.print(Text.assemble(("    ◦ ", FAINT), (title.ljust(52), TEXT),
                                    (f"{fmt_ago(row['updated_at'])} · {tail_path(row['cwd'], 28)}", FAINT)))
    console.print(Text("  ") + keycap("ctrl+r", ACCENT) + Text(" all conversations, searchable", style=MUTED))
    console.print()


def tool_detail(name: str, args: dict) -> Text | Syntax | None:
    """What a tool call was asked to do, in full: the command, the edit as a diff, the file written."""
    if name == "run_command":
        return Text("$ " + str(args.get("command", "")), style="bold")
    if name == "edit_file":
        diff = "\n".join([*("-" + row for row in str(args.get("old_string", "")).splitlines()),
                          *("+" + row for row in str(args.get("new_string", "")).splitlines())])
        return Syntax(diff, "diff", theme="ansi_dark", word_wrap=True)
    if name == "write_file":
        rows = str(args.get("content", "")).splitlines()
        body = "\n".join("+" + row for row in rows[:VERBOSE_OUTPUT_LINES])
        if len(rows) > VERBOSE_OUTPUT_LINES:
            body += f"\n… {len(rows) - VERBOSE_OUTPUT_LINES} more lines"
        return Syntax(body, "diff", theme="ansi_dark", word_wrap=True)
    return None


def print_transcript(turns: list[dict]) -> None:
    """The whole conversation in detail: prompts, thinking, every tool call with its input and output."""
    for turn in turns:
        console.print()
        if turn["role"] == "user":
            console.print(Text.assemble(("❯ ", f"bold {ACCENT}"), (turn["text"], "bold")))
            continue
        for part in turn["parts"]:
            if part["type"] == "reasoning":
                console.print(Text("∴ Thinking", style=f"italic {ACCENT}"))
                console.print(Padding(Text(part["text"], style=f"italic {MUTED}"), (0, 0, 0, 2)))
            elif part["type"] == "text":
                console.print(answer(part["text"]))
            else:
                console.print(tool_start_line(part["name"], part["args"]))
                detail = tool_detail(part["name"], part["args"])
                if detail is not None:
                    console.print(Padding(detail, (0, 0, 0, 5)))
                if part["output"] is None:
                    console.print(Text("  ╰ (stopped before it finished)", style=WARN))
                else:
                    console.print(tool_end_line(part["name"], part["output"], bool(part["ok"]), None, verbose=True))


def print_events(events: list[dict], full: bool = False, out: Console | None = None) -> None:
    """The activity log, one line per event; `full` adds arguments and output."""
    out = out or console
    for e in events:
        when = dt.datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
        d, kind = e["detail"], e["kind"]
        line = Text(f"  {when}  ", style=MUTED)
        if kind == "prompt":
            line.append("❯ ", style=f"bold {ACCENT}")
            line.append(" ".join(str(d.get("text", "")).split())[:160])
        elif kind == "tool":
            line.append("● ", style=ACCENT)
            line.append(TOOL_LABELS.get(e["name"], e["name"]), style="bold")
            line.append("  " + summarize_args(e["name"], d.get("args", {})), style=MUTED)
            line.append("  ✓" if e["ok"] else "  ✗", style=OK if e["ok"] else ERR)
            if e["ms"] is not None:
                line.append(f" {e['ms']} ms", style=MUTED)
        elif kind == "usage":
            line.append(turn_footer(d, 0).plain.strip(), style=MUTED)
        elif kind == "cwd":
            line.append("→ now in " + tail_path(d.get("path", ""), 70), style=ACCENT_2)
        elif kind == "error":
            line.append("✗ " + str(d.get("error", ""))[:300], style=ERR)
        elif kind == "interrupt":
            line.append("■ stopped by the user", style=WARN)
        else:
            line.append(f"{kind} {e['name']}".strip(), style=MUTED)
        out.print(line)
        if full and kind == "tool":
            args = {k: (v if len(str(v)) <= 400 else str(v)[:400] + "…") for k, v in d.get("args", {}).items()}
            out.print(Text(f"            args    {args}", style=MUTED))
            rows = str(d.get("output", "")).splitlines()
            for row in rows[:VERBOSE_OUTPUT_LINES]:
                out.print(Text("            " + row[:200], style=MUTED))
            if len(rows) > VERBOSE_OUTPUT_LINES:
                out.print(Text(f"            … {len(rows) - VERBOSE_OUTPUT_LINES} more lines", style=MUTED))


apply_theme()  # the default theme until the config names one


def commit_line(event: dict) -> Text:
    """The line under a turn that auto-commit (gitnet) committed, or why it did not."""
    if event.get("sha"):
        files = event.get("files") or []
        line = Text.assemble(("  ⎇ ", f"bold {ACCENT_2}"), ("committed ", MUTED), (event["sha"][:7], f"bold {TEXT}"),
                             ("  " + event.get("message", ""), TEXT),
                             (f"  · {len(files)} file{'s' if len(files) != 1 else ''}", MUTED))
    else:
        line = Text.assemble(("  ⎇ ", f"bold {WARN}"), (event.get("error") or "not committed", WARN))
    if event.get("skipped"):
        names = ", ".join(Path(p).name for p in event["skipped"][:4])
        line.append(f"\n    left out, they already had your changes: {names}", style=MUTED)
    return line


# ---------------------------------------------------------------- what the Telegram bot is doing
class FeedPrinter:
    """Turns the Telegram bot's live feed (`telegram.Feed`) into the lines a terminal turn
    would show: who wrote, the steps, diffs, questions and the answer. Says who it is
    for whenever the conversation it follows changes."""

    def __init__(self) -> None:
        self.chat = None

    def header(self, event: dict) -> Text:
        who = event.get("name") or "Telegram"
        return Text.assemble(("  ✈ ", f"bold {ACCENT}"), (who, f"bold {TEXT}"), ("  on Telegram", MUTED))

    def render(self, event: dict) -> list:
        kind, out = event.get("kind"), []
        if kind == "user" or event.get("chat") != self.chat:
            out.append(Text())
            out.append(self.header(event))
            self.chat = event.get("chat")
        if kind == "user":
            text = event.get("text", "")
            out.append(user_bubble(("➕ " if event.get("steer") else "") + text))
            if event.get("images"):
                out.append(Text(f"     {event['images']} image(s) attached", style=MUTED))
            out.append(Text())
        elif kind == "tool_start":
            args = event.get("args") or {}
            out.append(tool_start_line(event.get("tool", ""), args))
            if event.get("tool") == "run_command":
                out.append(Text("     $ " + str(args.get("command", "")), style=MUTED))
        elif kind == "tool_end":
            out.append(tool_end_line(event.get("tool", ""), event.get("output", ""), event.get("ok", True), event.get("ms")))
        elif kind == "diff":
            title = ("Created " if event.get("new") else "Edited ") + short_path(event.get("path", ""))
            out.append(diff_panel(event, title, MUTED, AUTO_DIFF_LINES))
        elif kind == "ask":
            action = event.get("action") or {}
            if action.get("kind") == "edit":
                title = ("Create " if action.get("new") else "Edit ") + short_path(action.get("path", "")) + "?"
                out.append(diff_panel(action, title + "  (asked on Telegram)", WARN, AUTO_DIFF_LINES))
            else:
                what = action.get("command") or action.get("url") or action.get("mcp_tool") or ""
                out.append(Text.assemble(("  ? ", f"bold {WARN}"), (str(what), TEXT), ("  asked on Telegram", MUTED)))
        elif kind == "answered":
            answer_ = event.get("answer")
            label = {"yes": ("✓ Allowed", OK), "always": ("✓ Allowed from now on", OK)}.get(answer_, ("✗ Declined", ERR))
            out.append(Text.assemble(("  ", ""), (label[0], f"bold {label[1]}"), ("  on Telegram", MUTED)))
        elif kind == "answer":
            out.append(answer(event.get("text", ""), mark=True))
        elif kind == "tasks":
            out.append(tasks_block(event.get("items") or []))
        elif kind in ("notice", "cwd"):
            out.append(Text("  " + str(event.get("text") or f"now in {event.get('path', '')}"), style=MUTED))
        elif kind == "commit":
            out.append(commit_line(event))
        elif kind == "done":
            bits = []
            if event.get("seconds") is not None:
                bits.append(fmt_duration(event["seconds"]))
            if event.get("tools"):
                bits.append(f"{event['tools']} step{'s' if event['tools'] != 1 else ''}")
            if event.get("stopped"):
                bits.append("stopped")
            if event.get("error"):
                out.append(Text("  ⚠ " + str(event["error"]), style=WARN))
            out.append(Text.assemble(("  ◦ ", FAINT), (" · ".join(bits) or "done", FAINT)))
            self.chat = None
        return out
