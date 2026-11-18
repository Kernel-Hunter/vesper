"""The terminal's look: themes switch every screen at once, and the drawing helpers stay exact."""

import json

from rich.console import Console

from vesper import onboarding, themes, tui, ui


def render(renderable, width: int = 80) -> str:
    con = Console(width=width, record=True, force_terminal=False, color_system=None)
    con.print(renderable)
    return con.export_text()


def test_a_theme_reaches_every_module_that_draws():
    ui.apply_theme("ember")
    try:
        assert ui.ACCENT == tui.ACCENT == onboarding.ACCENT == themes.THEMES["ember"].accent
        assert tui.FAINT == themes.THEMES["ember"].faint
    finally:
        ui.apply_theme("dusk")
    assert tui.ACCENT == themes.THEMES["dusk"].accent
    assert ui.apply_theme("no-such-theme").name in ("dusk", "daylight")  # unknown names fall back


def test_light_terminals_get_the_light_theme(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert themes.resolve(None).name == "daylight"
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert themes.resolve(None).name == "dusk"
    assert themes.resolve("ocean").name == "ocean"


def test_meter_fills_to_an_eighth_of_a_cell():
    assert ui.meter(0, 4) == "    "
    assert ui.meter(1, 4) == "████"
    assert ui.meter(0.5, 4) == "██  "
    assert ui.meter(1 / 32, 4) == "▏   "
    assert all(len(ui.meter(f / 100, 8)) == 8 for f in range(101))


def test_wordmark_rows_line_up():
    rows = ui.wordmark()
    assert len(rows) == 3 and len({r.cell_len for r in rows}) == 1
    assert set("".join(r.plain for r in rows)) <= set("█▀▄ ")


def test_diff_view_numbers_lines_and_drops_file_headers():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -3,3 +3,3 @@ def f():\n     a = 1\n-    b = 2\n+    b = 3\n     return a\n"
    out = render(ui.DiffView(diff))
    assert "a/x.py" not in out and "⋯ def f():" in out
    lines = [line.rstrip() for line in out.splitlines()]
    assert "   3       a = 1" in lines and "   4 -     b = 2" in lines and "   4 +     b = 3" in lines
    assert "   5       return a" in lines


def test_git_branch_reads_head(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feature/login\n")
    (tmp_path / "src").mkdir()
    assert ui.git_branch(tmp_path / "src") == "feature/login"
    assert ui.git_branch(tmp_path.parent / "not-a-repo-xyz") == ""


def test_theme_command_saves_the_choice():
    app = tui.App.__new__(tui.App)
    app.cfg = {}
    try:
        app.theme("forest")
        assert app.cfg["theme"] == "forest" and tui.ACCENT == themes.THEMES["forest"].accent
        saved = json.loads((onboarding.home() / "config.json").read_text())
        assert saved["theme"] == "forest"
        app.theme("")  # the list does not change anything
        assert ui.THEME.name == "forest"
    finally:
        ui.apply_theme("dusk")
