"""Draw docs/terminal.svg, the terminal screenshot in the README.

It uses the real drawing code from src/vesper/ui.py (banner, tool lines, approval panel)
with a scripted session, so the picture stays true to what `vesper` prints.
Run after changing the terminal UI: python packaging/make_terminal_svg.py
"""

from pathlib import Path

from rich.console import Console
from rich.text import Text

from vesper import ui

OUT = Path(__file__).resolve().parents[1] / "docs" / "terminal.svg"

DIFF = """--- a/src/budget.py
+++ b/src/budget.py
@@ -12,7 +12,7 @@ def monthly_total(rows):
     total = 0
     for row in rows:
-        total =+ row.amount
+        total += row.amount
     return total
"""


def prompt_line(con: Console, text: str) -> None:
    con.print(Text("─" * (con.width - 1), style=ui.FAINT))
    con.print(Text.assemble(("❯ ", f"bold {ui.ACCENT}"), (text, ui.TEXT)))


def main(theme: str = "dusk", out: Path = OUT) -> None:
    con = Console(record=True, width=92, force_terminal=True, color_system="truecolor", highlight=False)
    ui.console = con
    ui.apply_theme(theme)
    say = con.print

    ui.banner(Path.home() / "projects" / "budget-app", {"provider": "anthropic", "model": "claude-sonnet-5-5"}, "ask")
    prompt_line(con, "the monthly total test fails, fix it")
    say()
    steps = [
        ("run_command", {"command": "pytest -q"}, "exit code 1\n1 failed, 11 passed in 0.31s", 1400),
        ("grep", {"pattern": "def monthly_total"}, "src/budget.py:10: def monthly_total(rows):", None),
        ("read_file", {"path": "src/budget.py"}, "\n".join(f"{i:6}\tx" for i in range(1, 41)), None),
    ]
    for name, args, output, ms in steps:
        say(ui.tool_start_line(name, args))
        say(ui.tool_end_line(name, output, True, ms))
    say(Text("∴ Thought for 2.1s", style=f"italic {ui.FAINT}"))
    say(ui.answer("Line 14 uses `=+`, which assigns instead of adding. Fixing it:"))
    say(ui.tool_start_line("edit_file", {"path": "src/budget.py"}))
    say(ui.diff_panel({"diff": DIFF}, "Edit src/budget.py"))
    say(Text.assemble(("  Allow?  ", "bold"), ("y", f"bold {ui.OK}"), (" yes  ", ui.MUTED),
                      ("a", f"bold {ui.ACCENT}"), (" always allow edits this session  ", ui.MUTED),
                      ("n", f"bold {ui.ERR}"), (" no  ", ui.MUTED), ("[y] ", ui.MUTED), "y"))
    say(ui.tool_end_line("edit_file", "Edited src/budget.py", True, None))
    say(ui.tool_start_line("run_command", {"command": "pytest -q"}))
    say(ui.tool_end_line("run_command", "exit code 0\n12 passed in 0.29s", True, 1200))
    say(ui.answer("Fixed. `monthly_total` added with `=+`, so it kept only the last row. "
                  "All 12 tests pass now."))
    say(ui.turn_footer({"seconds": 14.2, "tools": 5, "input": 6200, "output": 410, "context": 9800,
                        "cost": 0.0213}, 200_000))
    say()
    prompt_line(con, "")
    say(Text.assemble((" ✦ ", f"bold {ui.ACCENT_3}"), ("claude-sonnet-5-5", ui.TEXT), ("  │  ", ui.FAINT),
                      ("ask", f"bold {ui.mode_color('ask')}"), ("  │  ", ui.FAINT), ("context ", ui.FAINT),
                      *ui.meter_parts(0.049, 8), (" 5%", ui.MUTED), ("  │  ", ui.FAINT),
                      ("~/projects/budget-app", ui.MUTED), ("  │  ", ui.FAINT), ("⎇ main", ui.MUTED)))

    out.parent.mkdir(exist_ok=True)
    con.save_svg(str(out), title="vesper")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
