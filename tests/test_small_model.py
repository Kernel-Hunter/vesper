"""Small-model routing: a cheaper model writes chat titles and summaries of long conversations."""

from langchain_core.language_models.fake_chat_models import FakeListChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import providers as prov
from vesper.coder import SUMMARY_OPEN, Session, text_of
from vesper.store import Store
from vesper.workspace import Workspace


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class Broken(FakeListChatModel):
    def _call(self, *args, **kwargs):
        raise RuntimeError("small model is down")

    def _stream(self, *args, **kwargs):
        raise RuntimeError("small model is down")


def session(tmp_path, *replies):
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "db" / "v.db")
    s = Session(ToolFake(disable_streaming=True, messages=iter(replies)), Workspace(proj, mode="yolo"), store)
    return s, store


def test_the_small_model_names_a_new_chat(tmp_path):
    s, store = session(tmp_path, AIMessage("Done."), AIMessage("Again."))
    s.small_llm = FakeListChatModel(responses=['"Fix the login redirect."'])
    events = list(s.ask("the login page sends me back to / after signing in, please fix it"))
    assert ("title", {"title": "Fix the login redirect"}) in events
    assert store.get_session(s.id)["title"] == "Fix the login redirect"
    events = list(s.ask("and the logout too"))  # only the first message names the chat
    assert not any(k == "title" for k, _ in events)
    store.close()


def test_without_a_small_model_the_first_words_stay_the_title(tmp_path):
    s, store = session(tmp_path, AIMessage("Done."))
    events = list(s.ask("rename the helper function"))
    assert not any(k == "title" for k, _ in events)
    assert store.get_session(s.id)["title"] == "rename the helper function"
    store.close()


def test_a_failing_small_model_keeps_the_first_words(tmp_path):
    s, store = session(tmp_path, AIMessage("Done."))
    s.small_llm = Broken(responses=["x"])
    events = list(s.ask("rename the helper function"))
    assert not any(k == "title" for k, _ in events)
    assert store.get_session(s.id)["title"] == "rename the helper function"
    store.close()


def test_summaries_use_the_small_model_and_fall_back_to_the_main_one(tmp_path):
    s, store = session(tmp_path, AIMessage("one"), AIMessage("two"), AIMessage("three"))
    for q in ("a", "b", "c"):
        list(s.ask(q))
    s.small_llm = FakeListChatModel(responses=["- small summary"])
    assert s.compact(keep_turns=1)
    assert text_of(s.history[0].content).startswith(SUMMARY_OPEN) and "small summary" in text_of(s.history[0].content)

    s2, store2 = session(tmp_path / "two", AIMessage("one"), AIMessage("two"), AIMessage("- main summary"))
    for q in ("a", "b"):
        list(s2.ask(q))
    s2.small_llm = Broken(responses=["x"])
    assert s2.compact(keep_turns=1)
    assert "main summary" in text_of(s2.history[0].content)
    store.close()
    store2.close()


def test_get_small_llm_is_none_until_both_are_set(monkeypatch):
    monkeypatch.delenv("VESPER_FAKE_LLM", raising=False)
    assert prov.get_small_llm("", "x") is None
    assert prov.get_small_llm("groq", " ") is None
    assert prov.get_small_llm("nope", "x") is None
    assert prov.get_small_llm("ollama", "llama3.2") is not None


def test_settings_api_saves_and_checks_the_small_model(client):
    r = client.post("/api/settings", json={"small_provider": "groq", "small_model": "llama-3.1-8b-instant"})
    assert r.status_code == 200
    s = client.get("/api/state").json()["settings"]
    assert (s["small_provider"], s["small_model"]) == ("groq", "llama-3.1-8b-instant")
    assert client.post("/api/settings", json={"small_provider": "nope"}).status_code == 400
    r = client.post("/api/settings", json={"small_provider": "", "small_model": ""})
    assert r.json()["settings"]["small_provider"] == ""
