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
    assert len(rows) == 4 and len({r.cell_len for r in rows}) == 1
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


def test_keycaps_bubbles_and_the_answer_star():
    cap = ui.keycap("y", ui.OK)
    assert cap.plain == " y " and all("on " in str(span.style) for span in cap.spans)
    assert "❯ fix the bug" in render(ui.user_bubble("fix the bug"))
    assert render(ui.answer("Done.", mark=True)).startswith("✦ Done.")
    assert render(ui.answer("Done.")).startswith("  Done.")
    r = ui.Renderer()
    assert r._answer("x") is not None and not r.marked


def test_the_terminal_background_answer_is_read():
    assert themes.background_from_reply(b"\x1b]11;rgb:0000/0000/0000\x1b\\\x1b[?62;22c") is False
    assert themes.background_from_reply(b"\x1b]11;rgb:ffff/fafa/f0f0\x07") is True
    assert themes.background_from_reply(b"\x1b]11;rgb:1e/1e/2e\x07") is False
    assert themes.background_from_reply(b"\x1b[?1;2c") is None  # the terminal did not say


def test_a_light_theme_is_not_used_on_a_dark_terminal(monkeypatch):
    monkeypatch.setattr(themes, "background_is_light", lambda: False)
    assert themes.resolve("daylight").name == "daylight-dark" and themes.swapped_from is None  # its dark version
    assert themes.resolve("ocean").name == "ocean" and themes.swapped_from is None
    monkeypatch.setattr(themes, "background_is_light", lambda: True)
    assert themes.resolve("ocean").name == "daylight" and themes.swapped_from.name == "ocean"
    assert themes.resolve("daylight").name == "daylight"
    assert themes.resolve("daylight-dark").name == "daylight" and themes.swapped_from is None
    monkeypatch.setattr(themes, "background_is_light", lambda: None)  # unknown: trust the choice
    assert themes.resolve("daylight").name == "daylight" and themes.swapped_from is None
    ui.apply_theme(None)


def test_every_theme_is_readable_on_its_own_background():
    def lum(h):
        def f(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def contrast(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for p in themes.THEMES.values():
        for bg in (("#000000", "#1e1e1e") if p.dark else ("#ffffff", "#f5f5f5")):
            assert contrast(p.text, bg) >= 7, (p.name, "text")
            assert contrast(p.muted, bg) >= 4.5, (p.name, "muted")
            assert contrast(p.faint, bg) >= 3.4, (p.name, "faint")
