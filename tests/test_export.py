"""Session export (export.py): a conversation as Markdown or HTML. Offline."""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vesper import export, tui
from vesper.store import Store

MESSAGES = [
    HumanMessage("Why does `parse` fail?\n<script>alert(1)</script>"),
    AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "src/parse.py"}, "id": "t1"}],
              additional_kwargs={"reasoning_content": "Look at the file first."}),
    ToolMessage("1  def parse(x):\n2      return ```x```", tool_call_id="t1", name="read_file"),
    AIMessage("", tool_calls=[{"name": "run_command", "args": {"command": "pytest -q"}, "id": "t2"}]),
    ToolMessage("Error: 1 failed", tool_call_id="t2", name="run_command", status="error"),
    AIMessage("It returns **backticks**. See [docs](javascript:alert(1)) and <b>this</b>."),
]
ROW = {"id": "abc123def456", "title": "Fix the parser", "cwd": "/proj", "model": "m1", "created_at": 1764000000}


def test_markdown_keeps_messages_steps_and_fences():
    md = export.to_markdown(ROW, MESSAGES)
    assert md.startswith("# Fix the parser\n")
    assert "## You\n\nWhy does `parse` fail?" in md and "## Vesper" in md
    assert "<summary>Read src/parse.py</summary>" in md
    assert "````\n1  def parse(x):" in md  # a fence longer than the backticks inside
    assert "(failed)" in md and "Look at the file" not in md
    assert "Look at the file first." in export.to_markdown(ROW, MESSAGES, reasoning=True)


def test_html_is_self_contained_and_runs_nothing():
    page = export.to_html(ROW, MESSAGES)
    assert page.startswith("<!doctype html>") and "Content-Security-Policy" in page
    assert "<script>" not in page and "&lt;script&gt;" in page  # the user's text is escaped
    assert "<strong>backticks</strong>" in page and "<b>" not in page  # markdown yes, raw HTML no
    assert 'href="javascript' not in page
    assert '<details class="failed"><summary>Run pytest -q (failed)</summary>' in page
    head = page.split("<body>")[0]
    assert "src=" not in head and "<link" not in head  # no outside resources


def test_write_names_the_file_and_sessions_export_works(tmp_path, capsys, monkeypatch):
    path = export.write(ROW, MESSAGES, "md", tmp_path)
    assert path.name == "vesper-fix-the-parser-abc123de.md" and path.read_text().startswith("# Fix")
    store = Store()
    sid = store.create_session(str(tmp_path), "fake", "m1")
    store.save_messages(sid, MESSAGES)
    store.update_session(sid, title="Fix the parser")
    store.close()
    monkeypatch.chdir(tmp_path)
    assert tui.main(["sessions", "export", sid[:6], "--html"]) == 0
    assert (tmp_path / f"vesper-fix-the-parser-{sid[:8]}.html").exists()
    assert tui.main(["sessions", "export", sid, "-o", "-"]) == 0
    assert "## You" in capsys.readouterr().out
    assert tui.main(["sessions", "export", "zzzz"]) == 1


def test_the_app_endpoint_returns_the_file(tmp_path):
    from fastapi.testclient import TestClient

    from vesper import server

    store = Store()
    sid = store.create_session(str(tmp_path), "fake", "m1")
    store.save_messages(sid, MESSAGES)
    store.close()
    client = TestClient(server.create_app("t", engine=server.Engine()), headers={"X-Token": "t", "Host": "127.0.0.1"})
    got = client.get(f"/api/sessions/{sid}/export?format=html").json()
    assert got["name"].endswith(".html") and got["text"].startswith("<!doctype html>")
    assert client.get(f"/api/sessions/{sid}/export?format=pdf").status_code == 400
    assert client.get("/api/sessions/nope00000000/export").status_code == 404
