"""Closing and coming back: answers cut off by a quit are marked, and long histories save fast."""

import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vesper.store import Store


def long_history(turns: int) -> list:
    out = []
    for i in range(turns):
        out += [HumanMessage(f"question {i}"),
                AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": f"c{i}"}]),
                ToolMessage("x" * 2000, tool_call_id=f"c{i}"), AIMessage(f"answer {i}")]
    return out


def test_saving_writes_only_what_changed_and_stays_correct(tmp_path):
    store = Store(tmp_path / "v.db")
    sid = store.create_session(tmp_path)
    history = long_history(1000)
    store.save_messages(sid, history)
    history = history + [AIMessage("one more step")]
    started = time.perf_counter()
    store.save_messages(sid, history)
    assert time.perf_counter() - started < 0.02  # 4,000 messages: a step costs about as much as a short chat
    loaded = store.load_messages(sid)
    assert len(loaded) == 4001 and loaded[-1].content == "one more step"

    history = history[:6] + [AIMessage("rewound")]  # rewind, then a new answer
    store.save_messages(sid, history)
    loaded = store.load_messages(sid)
    assert len(loaded) == 7 and loaded[-1].content == "rewound"
    store.close()


def test_another_process_writing_the_same_session_forces_a_full_rewrite(tmp_path):
    a = Store(tmp_path / "v.db")
    sid = a.create_session(tmp_path)
    base = long_history(3)
    a.save_messages(sid, base)
    time.sleep(0.01)
    b = Store(tmp_path / "v.db")
    b.save_messages(sid, [HumanMessage("only the terminal's message")])
    time.sleep(0.01)
    a.save_messages(sid, base + [AIMessage("the app's next step")])
    loaded = a.load_messages(sid)
    assert len(loaded) == len(base) + 1 and loaded[0].content == "question 0" and loaded[-1].content == "the app's next step"
    a.close()
    b.close()


def test_cut_off_means_a_question_that_never_finished(tmp_path):
    store = Store(tmp_path / "v.db")
    sid = store.create_session(tmp_path)
    assert not store.cut_off(sid)
    store.add_event(sid, "prompt", detail={"text": "a"})
    assert store.cut_off(sid)  # the app or terminal died before the answer ended
    store.add_event(sid, "usage", detail={})
    assert not store.cut_off(sid)  # finished, or stopped on purpose
    store.add_event(sid, "prompt", detail={"text": "b"})
    store.add_event(sid, "interrupt", "quit")
    store.add_event(sid, "usage", detail={})
    assert store.cut_off(sid)  # the app quit while it ran, then saved what it had
    store.add_event(sid, "prompt", detail={"text": "continue"})
    store.add_event(sid, "usage", detail={})
    assert not store.cut_off(sid)
    store.add_event(sid, "prompt", detail={"text": "c"})
    store.add_event(sid, "interrupt")  # Ctrl+C in the terminal: the user's choice
    assert not store.cut_off(sid)
    store.close()


def test_busy_endpoint_and_cut_off_in_saved_session(client, tmp_path):
    assert client.get("/api/busy").json() == {"working": 0}
    db = client.app.state.engine.history_db()
    sid = db.create_session(tmp_path)
    db.save_messages(sid, [HumanMessage("build it")])
    db.add_event(sid, "prompt", detail={"text": "build it"})
    assert client.get(f"/api/sessions/{sid}").json()["cut_off"] is True
    db.add_event(sid, "usage", detail={})
    assert client.get(f"/api/sessions/{sid}").json()["cut_off"] is False
