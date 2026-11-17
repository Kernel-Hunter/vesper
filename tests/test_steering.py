"""Steering: a message sent while the agent works joins the running turn at its next step."""

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vesper.coder import STEER_KEY, TURN_ID_PREFIX, Session, transcript
from vesper.store import Store
from vesper.workspace import Workspace


class ToolFake(GenericFakeChatModel):
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        type(self).seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def session(tmp_path, *replies):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("hi\n")
    store = Store(tmp_path / "db" / "v.db")
    ToolFake.seen = []
    llm = ToolFake(disable_streaming=True, messages=iter(replies))
    return Session(llm, Workspace(proj), store), store


def test_a_message_sent_during_a_turn_reaches_the_next_model_call(tmp_path):
    s, store = session(tmp_path, AIMessage("", tool_calls=[{"name": "list_files", "args": {"path": "."}, "id": "l1"}]),
                       AIMessage("Done, and I used the tests folder too."))
    assert s.steer("too early") is False  # no turn is running yet
    events = []
    for kind, data in s.ask("look around"):
        events.append((kind, data))
        if kind == "tool_start":
            assert s.steer("also check the tests folder") is True
    steer = [d for k, d in events if k == "steer"]
    assert steer and steer[0]["texts"] == ["also check the tests folder"] and steer[0]["id"].startswith(TURN_ID_PREFIX)
    assert not any(k == "unsteered" for k, _ in events)
    # The second model call saw the tool result and then the new message.
    second = ToolFake.seen[1]
    assert isinstance(second[-2], ToolMessage) and isinstance(second[-1], HumanMessage)
    assert second[-1].content == "also check the tests folder"
    # It is saved in order, and the chat shows it as the user's message with its own turn.
    saved = store.load_messages(s.id)
    assert [type(m).__name__ for m in saved] == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage", "AIMessage"]
    assert saved[3].additional_kwargs[STEER_KEY] == ["also check the tests folder"]
    turns = transcript(saved)
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[2]["text"] == "also check the tests folder" and turns[2]["turn"] == steer[0]["id"]
    assert s.steer("after the end") is False
    store.close()


def test_a_message_sent_after_the_last_model_call_comes_back_unused(tmp_path):
    s, store = session(tmp_path, AIMessage("All done."))
    events = []
    for kind, data in s.ask("hello"):
        events.append((kind, data))
        if kind == "token":
            s.steer("one more thing")
    assert ("unsteered", {"texts": ["one more thing"]}) in events
    assert events[-1] == ("done", {})
    assert [type(m).__name__ for m in store.load_messages(s.id)] == ["HumanMessage", "AIMessage"]
    store.close()


def test_steer_endpoint_validates_and_reports_when_nothing_runs(client):
    r = client.post("/api/chats/c1/steer", json={"text": "hi"})
    assert r.status_code == 200 and r.json() == {"accepted": False}
    assert client.post("/api/chats/c1/steer", json={"text": "  "}).status_code == 400
    assert client.post("/api/chats/c1/steer", json={"nope": 1}).status_code == 400
    assert client.post("/api/chats/c1/steer", json={"text": "x"}, headers={"X-Token": "wrong"}).status_code == 401
