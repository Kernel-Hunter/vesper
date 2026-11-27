"""Language-server errors added to write_file / edit_file results (fake server, offline)."""
import sys
import time
from pathlib import Path

import pytest

from vesper import diagnostics
from vesper.workspace import Workspace

FAKE = Path(__file__).with_name("fake_lsp.py")


@pytest.fixture
def lsp(monkeypatch, tmp_path):
    """A fresh manager whose Python server is the fake one."""
    monkeypatch.setattr(diagnostics, "find_command", lambda spec, root: [sys.executable, str(FAKE)])
    monkeypatch.setattr(diagnostics, "FIRST_WAIT", 5.0)
    monkeypatch.setattr(diagnostics, "WAIT", 2.0)
    m = diagnostics.Manager()
    monkeypatch.setattr(diagnostics, "_manager", m)
    (tmp_path / "pyproject.toml").write_text("")
    yield Workspace(tmp_path, mode="yolo")
    m.close()


def test_a_new_error_goes_back_with_the_edit(lsp):
    out = lsp.write_file("a.py", "x = 1\ny = ERROR\n")
    assert out.startswith("Created a.py (2 lines).")
    assert "reports 1 new error in a.py:" in out
    assert "2:5 error: y = ERROR" in out


def test_errors_already_there_are_left_out(lsp, tmp_path):
    (tmp_path / "b.py").write_text("old = ERROR\nok = 1\n")
    out = lsp.edit_file("b.py", "ok = 1", "ok = 1\nnew = ERROR WARN")
    assert "1 new error" in out and "new = ERROR WARN" in out
    assert "old = ERROR" not in out
    assert "1 error already in the file before this edit not shown" in out
    assert "warn: " not in out  # warnings are not reported


def test_a_clean_edit_adds_nothing(lsp, tmp_path):
    (tmp_path / "c.py").write_text("a = ERROR\n")
    assert lsp.edit_file("c.py", "a = ERROR", "a = 2") == "Edited c.py (1 change)."
    assert lsp.edit_file("c.py", "a = 2", "a = 3") == "Edited c.py (1 change)."  # server already running


def test_other_languages_and_the_off_switch(lsp):
    assert lsp.write_file("notes.md", "ERROR\n") == "Created notes.md (1 lines)."
    diagnostics.set_enabled(False)
    assert lsp.write_file("d.py", "ERROR\n") == "Created d.py (1 lines)."
    diagnostics.set_enabled(True)
    assert "new error" in lsp.write_file("e.py", "ERROR\n")


def test_a_silent_server_times_out_quietly(lsp, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "silent")
    monkeypatch.setattr(diagnostics, "FIRST_WAIT", 0.5)
    monkeypatch.setattr(diagnostics, "WAIT", 0.3)
    started = time.monotonic()
    assert lsp.write_file("f.py", "ERROR\n") == "Created f.py (1 lines)."
    assert time.monotonic() - started < 5


def test_a_crashing_server_is_given_up(lsp, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_LSP_MODE", "crash")
    for name in ("g.py", "h.py", "i.py"):
        assert lsp.write_file(name, "ERROR\n") == f"Created {name} (1 lines)."
    assert diagnostics.manager().server_for(tmp_path / "j.py") == (None, False)  # failed twice: stop trying


def test_no_server_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "find_command", lambda spec, root: None)
    monkeypatch.setattr(diagnostics, "_manager", diagnostics.Manager())
    ws = Workspace(tmp_path, mode="yolo")
    assert ws.write_file("k.py", "ERROR\n") == "Created k.py (1 lines)."
    assert all(state == "not installed" for _, state in diagnostics.status(tmp_path))


def test_the_report_is_capped():
    errs = [{"range": {"start": {"line": i, "character": 0}}, "message": "m" * 300, "severity": 1} for i in range(30)]
    text = diagnostics.format_errors(errs, "x.py", "pyright")
    assert "30 new errors" in text and "more" in text.splitlines()[-1]
    assert len(text) <= diagnostics.MAX_CHARS


def test_paths_are_compared_not_uri_spellings(tmp_path):
    p = tmp_path / "a b.py"
    assert diagnostics._uri_key(p.as_uri()) == diagnostics._key(p)


def test_settings_point_pyright_at_the_project_python(tmp_path):
    s = diagnostics.Server.__new__(diagnostics.Server)
    s.root = tmp_path
    assert s._setting("python") == {}
    assert s._setting("python.analysis")["diagnosticSeverityOverrides"]["reportMissingImports"] == "none"
    bin_dir = tmp_path / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    (bin_dir / ("python.exe" if sys.platform == "win32" else "python")).write_text("")
    assert s._setting("python")["pythonPath"].startswith(str(tmp_path))
    assert s._setting("python.analysis")["diagnosticSeverityOverrides"] == {}


def test_no_before_picture_means_no_report(lsp, monkeypatch, tmp_path):
    """If the old errors are not known in time, old errors would look new: say nothing."""
    monkeypatch.setenv("FAKE_LSP_MODE", "slow-first")
    monkeypatch.setattr(diagnostics, "WAIT", 0.5)
    monkeypatch.setattr(diagnostics, "FIRST_WAIT", 0.5)
    (tmp_path / "s.py").write_text("old = ERROR\nok = 1\n")
    assert lsp.edit_file("s.py", "ok = 1", "ok = 2") == "Edited s.py (1 change)."


def test_each_edit_closes_the_document(lsp, tmp_path):
    """The fake server exits if a document is opened twice without a close in between."""
    (tmp_path / "t.py").write_text("a = 1\n")
    for n in range(3):
        out = lsp.edit_file("t.py", f"a = {n + 1}", f"a = {n + 2}\nb{n} = ERROR")
        assert f"b{n} = ERROR" in out, out
    assert diagnostics.manager().running()


def test_plan_mode_starts_no_server(lsp, tmp_path):
    lsp.mode = "plan"
    with pytest.raises(Exception):
        lsp.write_file("p.py", "ERROR\n")
    assert diagnostics.manager().running() == []


def test_a_loose_file_never_roots_at_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".git").mkdir()
    spec = diagnostics.spec_for(Path("x.py"))
    assert diagnostics.project_root(tmp_path / "loose" / "x.py", spec) == tmp_path / "loose"


def test_a_server_that_only_answers_requests(lsp, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_LSP_MODE", "pull")
    (tmp_path / "u.py").write_text("old = ERROR\nok = 1\n")
    out = lsp.edit_file("u.py", "ok = 1", "ok = ERROR")
    assert "1 new error" in out and "2:6 error: ok = ERROR" in out and "old = ERROR" not in out


def test_typescript_7_serves_itself(tmp_path):
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    tsc = bin_dir / ("tsc.cmd" if sys.platform == "win32" else "tsc")
    tsc.write_text("")
    tsc.chmod(0o755)
    (tmp_path / "node_modules" / "typescript").mkdir()
    (tmp_path / "node_modules" / "typescript" / "package.json").write_text('{"version": "7.0.2"}')
    spec = diagnostics.spec_for(Path("a.ts"))
    cmd = diagnostics.find_command(spec, tmp_path)
    assert cmd and cmd[1:] == ["--lsp", "--stdio"]
    (tmp_path / "node_modules" / "typescript" / "package.json").write_text('{"version": "5.6.0"}')
    cmd = diagnostics.find_command(spec, tmp_path)
    assert not cmd or cmd[1:] != ["--lsp", "--stdio"]
