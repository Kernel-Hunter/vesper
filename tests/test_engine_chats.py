"""Desktop engine: attachments, stop, rename, retry, delete, and settings shared with the terminal."""

import base64
import json
import os

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class Recorder(GenericFakeChatModel):
    """Answers from a script and remembers what it was sent."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.fixture
def engine(server, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "notes.md").write_text("The secret word is marmalade.\n")
    (proj / "pkg" / "a.py").write_text("x = 1\n")
    (proj / "shot.png").write_bytes(PNG)
    e = server.Engine()
    e.update({"workspace": str(proj), "agent_mode": "ask"})
    llm = Recorder(messages=iter([AIMessage(f"answer {i}") for i in range(10)]), seen=[], disable_streaming=True)
    monkeypatch.setattr(e, "llm", lambda: llm)
    e.test_llm = llm
    return e


def test_files_folders_and_images_reach_the_model_once(engine):
    pasted = {"name": "clip.png", "mime": "image/png", "data": base64.b64encode(PNG).decode()}
    events = list(engine.stream_answer("look", "c1", [], attachments=["notes.md", "pkg", "shot.png", "nope.txt"],
                                       images=[pasted, {"name": "bad", "mime": "text/html", "data": "eA=="}]))
    attached = next(d for e, d in events if e == "attached")
    assert attached["names"] == ["notes.md", "pkg", "shot.png", "clip.png"]
    assert [p.split(":")[0] for p in attached["problems"]] == ["nope.txt", "bad"]
    sent = engine.test_llm.seen[0][-1]
    assert isinstance(sent, HumanMessage)
    text = sent.content[0]["text"]
    assert text.startswith("look") and "marmalade" in text and '<folder path="pkg">' in text and "a.py" in text
    assert [b["type"] for b in sent.content[1:]] == ["image", "image"]
    # Later turns do not carry the pictures again.
    list(engine.stream_answer("again", "c1", []))
    history = engine.test_llm.seen[1]
    assert not any(isinstance(m.content, list) and any(b.get("type") == "image" for b in m.content) for m in history)
    assert "[image shot.png was shown to you in this message]" in str(history[1].content)
    # The saved transcript shows what was attached, not the file contents.
    sid = next(d for e, d in events if e == "session")["id"]
    first = engine.saved_session(sid)["turns"][0]
    assert first.pop("turn").startswith("vesper-turn-")
    assert first == {"role": "user", "text": "look", "attachments": ["notes.md", "pkg", "shot.png", "clip.png"]}


def test_context_report_and_last_usage_survive_reopening(engine, client, server):
    events = list(engine.stream_answer('hello', 'context-chat', []))
    sid = next(data['id'] for event, data in events if event == 'session')
    usage = next(data for event, data in events if event == 'usage')
    assert usage['context_report']['used'] > usage['context_report']['history']
    app = client.__class__(server.create_app('t', engine))
    app.headers['X-Token'] = 't'
    saved = app.get(f'/api/sessions/{sid}').json()
    assert saved['context_report']['used'] == usage['context_report']['used']
    assert saved['last_usage']['input'] == usage['input']
    assert saved['last_usage']['output'] == usage['output']


def test_retry_forgets_the_last_answer(engine):
    list(engine.stream_answer("first", "c2", []))
    list(engine.stream_answer("second", "c2", []))
    list(engine.stream_answer("second", "c2", [], retry=True))
    asked = [m.content for m in engine.test_llm.seen[-1] if isinstance(m, HumanMessage)]
    assert asked == ["first", "second"]


def test_rename_and_delete_reach_the_database(engine, client, server):
    events = list(engine.stream_answer("hello", "c3", []))
    sid = next(d for e, d in events if e == "session")["id"]
    app_client = client.__class__(server.create_app("t", engine))
    app_client.headers["X-Token"] = "t"
    assert app_client.post(f"/api/sessions/{sid}/rename", json={"title": "  My   chat "}).status_code == 200
    assert app_client.get(f"/api/sessions/{sid}").json()["title"] == "My chat"
    assert app_client.delete(f"/api/sessions/{sid}").status_code == 200
    assert app_client.get(f"/api/sessions/{sid}").status_code == 404
    assert app_client.delete(f"/api/sessions/{sid}").status_code == 404          # not silently ok
    assert app_client.delete("/api/sessions/not-an-id").status_code == 400
    assert app_client.post("/api/chats/c3/stop").json() == {"stopped": False}   # nothing running


def test_history_search_returns_matching_session_metadata(engine, client, server):
    first = list(engine.stream_answer("a distinctive question", "search-first", []))
    sid = next(d for e, d in first if e == "session")["id"]
    list(engine.stream_answer("another chat", "search-second", []))
    app = client.__class__(server.create_app("t", engine))
    app.headers["X-Token"] = "t"
    found = app.get("/api/sessions", params={"q": "DISTINCTIVE"}).json()["sessions"]
    assert [row["id"] for row in found] == [sid]
    assert "turns" not in found[0] and "messages" in found[0]
    assert app.get("/api/sessions", params={"q": "x" * 201}).status_code == 400


def test_sidebar_metadata_and_fork_persist_without_copying_undo(engine, client, server):
    events = list(engine.stream_answer("hello", "original", []))
    sid = next(d for e, d in events if e == "session")["id"]
    db = engine.history_db()
    db.add_checkpoint(sid, 1, "/example", b"old")
    app = client.__class__(server.create_app("t", engine))
    app.headers["X-Token"] = "t"
    url = f"/api/sessions/{sid}/sidebar"
    changes = {"pinned": True, "unread": True, "completed": True, "section": "Projects", "position": 2}
    assert app.post(url, json=changes).status_code == 200
    row = next(r for r in app.get("/api/sessions").json()["sessions"] if r["id"] == sid)
    assert all(row[k] == v for k, v in changes.items())
    for bad in ({"pinned": 1}, {"section": "bad\nname"}, {"position": -1}, {"unknown": True}):
        assert app.post(url, json=bad).status_code == 400
    fork = app.post(f"/api/sessions/{sid}/fork")
    assert fork.status_code == 200
    fork_id = fork.json()["id"]
    assert fork_id != sid
    assert db.load_messages(fork_id) == db.load_messages(sid)
    assert db.checkpoints(fork_id) == []
    assert db.get_session(fork_id)["cwd"] == db.get_session(sid)["cwd"]
    assert app.post(url, json={"archived": True}).status_code == 200
    assert next(r for r in engine.saved_sessions() if r["id"] == sid)["archived"] == 1
    from vesper.store import Store
    with_store = Store(db.path)
    try:
        row = next(r for r in with_store.list_sessions() if r["id"] == sid)
        assert row["pinned"] == row["archived"] == row["unread"] == 1
        assert row["section"] == "Projects"
    finally:
        with_store.close()


def test_stop_ends_a_running_command(engine, server, monkeypatch):
    from vesper.workspace import Workspace

    started = []
    real_run = Workspace.run_command

    def slow(self, command, *a, **k):
        started.append(command)
        engine.stop_chat("c4")
        return real_run(self, command, *a, **k)

    monkeypatch.setattr(Workspace, "run_command", slow)
    engine.update({"agent_mode": "yolo"})
    engine.test_llm.messages = iter([
        AIMessage("", tool_calls=[{"name": "run_command", "args": {"command": "sleep 20"}, "id": "r1"}]),
        AIMessage("never"),
    ])
    import time

    t = time.monotonic()
    events = list(engine.stream_answer("run it", "c4", []))
    assert started, events
    assert time.monotonic() - t < 5 and events[-1] == ("done", {})
    assert not any(e == "token" and "never" in d["text"] for e, d in events)
    assert engine.stop_chat("c4") is False  # nothing left running


def test_settings_saved_by_the_terminal_are_seen(engine, server):
    cfg = server.CONFIG
    data = json.loads(cfg.read_text())
    data.update(provider="groq", model="llama-x", thinking="high")
    cfg.write_text(json.dumps(data))
    os.utime(cfg, ns=(1, 1))
    engine.sync_settings()
    assert (engine.settings["provider"], engine.settings["model"], engine.settings["thinking"]) == ("groq", "llama-x", "high")
    # A desktop change writes only what changed: the terminal's values stay.
    engine.update({"agent_mode": "plan"})
    saved = json.loads(cfg.read_text())
    assert (saved["provider"], saved["model"], saved["agent_mode"]) == ("groq", "llama-x", "plan")


def test_parallel_chats_keep_folders_and_stop_independent(engine, server, client, tmp_path, monkeypatch):
    import concurrent.futures
    import threading

    from vesper.coder import Session

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    started = {str(first): threading.Event(), str(second): threading.Event()}
    release = threading.Event()

    def ask(self, question, **kwargs):
        folder = str(self.workspace.root)
        started[folder].set()
        assert release.wait(10)
        yield "token", {"text": question}

    monkeypatch.setattr(Session, "ask", ask)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(lambda: list(engine.stream_answer("one", "one", workspace=str(first))))
        assert started[str(first)].wait(5)
        engine.update({"workspace": str(second)})
        two = pool.submit(lambda: list(engine.stream_answer("two", "two", workspace=str(second))))
        try:
            assert started[str(second)].wait(5), "second chat did not start while the first was running"
            assert engine._sessions["one"]["session"].workspace.root == first
            assert engine._sessions["two"]["session"].workspace.root == second
            busy = list(engine.stream_answer("duplicate", "one"))
            assert any(event == "error" for event, _ in busy)
            sid = engine._sessions["one"]["session"].id
            engine.history_db().create_session(first, session_id=sid)
            engine.history_db().save_messages(sid, [HumanMessage("saved")])
            assert next(row for row in engine.saved_sessions() if row["id"] == sid)["working"] is True
            alias = list(engine.stream_answer("duplicate", "other-window", session_id=sid))
            assert any(event == "error" for event, _ in alias)
            app = client.__class__(server.create_app("t", engine))
            app.headers["X-Token"] = "t"
            assert app.post(f"/api/sessions/{sid}/fork").status_code == 409
            with pytest.raises(server.HTTPException) as error:
                engine.delete_saved_session(sid)
            assert error.value.status_code == 409
            assert engine.stop_chat("one")
            assert not engine._sessions["two"]["stop"] is engine._sessions["one"]["stop"]
        finally:
            release.set()
        one_events = one.result(timeout=10)
        assert not any(e == "token" for e, _ in one_events)
        assert any(e == "saved" and d["id"] == sid and d["updated_at"] > 0 for e, d in one_events)
        assert next(row for row in engine.saved_sessions() if row["id"] == sid)["working"] is False
        assert ("token", {"text": "two"}) in two.result(timeout=10)


def test_stopping_one_chat_refuses_its_pending_approval(engine, monkeypatch):
    import concurrent.futures
    import threading

    from vesper.coder import Session

    waiting = threading.Event()
    answers = []

    def ask(self, question, **kwargs):
        waiting.set()
        answers.append(self.workspace.approve({"kind": "run", "command": "example"}))
        yield "done", {}

    monkeypatch.setattr(Session, "ask", ask)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        result = pool.submit(lambda: list(engine.stream_answer("ask", "approval")))
        assert waiting.wait(5)
        # Wait for the approval slot, not just the worker reaching the callback.
        import time
        deadline = time.monotonic() + 5
        while not engine._approvals and time.monotonic() < deadline:
            time.sleep(0.01)
        assert engine._approvals
        assert engine.stop_chat("approval")
        result.result(timeout=5)
    assert answers == ["no"]


def test_stop_ends_the_answer_while_the_model_is_still_thinking(engine, monkeypatch):
    """Stop must work during a slow model call, not only between steps, and the chat is usable after."""
    import concurrent.futures
    import threading
    import time

    from vesper.coder import Session

    calling, release = threading.Event(), threading.Event()
    real_ask = Session.ask

    def slow_ask(self, question, **kwargs):
        if question == "slow":
            calling.set()
            release.wait(10)  # a model call that takes its time
            yield "token", {"text": "late"}
            return
        yield from real_ask(self, question, **kwargs)

    monkeypatch.setattr(Session, "ask", slow_ask)
    engine.test_llm.messages = iter([AIMessage("next answer")])
    with concurrent.futures.ThreadPoolExecutor() as pool:
        first = pool.submit(lambda: list(engine.stream_answer("slow", "c9", [])))
        assert calling.wait(5)
        t = time.monotonic()
        assert engine.stop_chat("c9")
        events = first.result(timeout=3)
        assert time.monotonic() - t < 2  # ended at once, without waiting for the model
        assert ("stopped", {}) in events and events[-1] == ("done", {})
        assert not any(e == "token" and d["text"] == "late" for e, d in events)
        second = pool.submit(lambda: list(engine.stream_answer("again", "c9", [])))
        time.sleep(0.2)
        release.set()  # the stopped call returns; the next message goes ahead instead of failing
        events = second.result(timeout=10)
    assert not any(e == "error" for e, _ in events), events
    assert ("token", {"text": "next answer"}) in events


def test_quitting_the_app_stops_running_answers_and_waits_briefly(engine, monkeypatch):
    import concurrent.futures
    import threading
    import time

    from vesper.coder import Session

    calling, release = threading.Event(), threading.Event()

    def slow_ask(self, question, **kwargs):
        calling.set()
        release.wait(10)
        yield "token", {"text": "late"}

    monkeypatch.setattr(Session, "ask", slow_ask)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        run = pool.submit(lambda: list(engine.stream_answer("slow", "q1", [])))
        assert calling.wait(5)
        t = time.monotonic()
        engine.shutdown(timeout=0.5)  # a model call that never returns cannot hold up quitting
        assert time.monotonic() - t < 1.5
        release.set()
        assert ("stopped", {}) in run.result(timeout=5)
    engine.shutdown(timeout=0.5)  # nothing running: returns at once


def test_undo_and_rewind_wait_for_a_stopped_answer_instead_of_failing(engine, monkeypatch):
    import concurrent.futures
    import threading
    import time

    from vesper.coder import Session

    calling, release = threading.Event(), threading.Event()

    def slow_ask(self, question, **kwargs):
        calling.set()
        release.wait(10)
        yield "token", {"text": "late"}

    monkeypatch.setattr(Session, "ask", slow_ask)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        run = pool.submit(lambda: list(engine.stream_answer("slow", "r1", [])))
        assert calling.wait(5)
        assert engine.stop_chat("r1")
        idle = pool.submit(engine._idle_session, "r1", None)  # what Edit, Rewind, Undo and Compact call
        time.sleep(0.3)
        assert not idle.done()  # waits for the stopped model call instead of answering 409
        release.set()
        entry, _ = idle.result(timeout=5)
        entry["lock"].release()
        run.result(timeout=5)
    entry = engine._folder_session("r1", [], None)
    assert not entry.get("stopping")
