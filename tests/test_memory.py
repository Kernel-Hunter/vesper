"""Memory (memory.py): notes kept between conversations. Offline."""
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import memory, prompts
from vesper.coder import Session
from vesper.workspace import Workspace


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "folder", lambda: tmp_path / "memory")
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def test_notes_are_kept_per_project_and_for_the_user(mem, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    memory.add(mem, "Tests run with pytest -q", "project")
    memory.add(mem, "Prefers short answers", "user")
    memory.add(mem, "Tests run with pytest -q", "project")  # not twice
    assert [s for s, _ in memory.listing(mem)] == ["user", "project"]
    assert [s for s, _ in memory.listing(other)] == ["user"]  # the user's notes follow everywhere
    section = prompts.agent_system_prompt(mem)
    assert "# Memory" in section and "Tests run with pytest -q" in section and "Prefers short answers" in section
    assert "Tests run" not in prompts.agent_system_prompt(other)
    assert memory.forget(mem, 2).startswith("Tests run with pytest -q") and len(memory.listing(mem)) == 1
    assert memory.forget(mem, 9) is None


def test_the_remember_tool_asks_and_saves(mem):
    asked = []
    ws = Workspace(mem, mode="ask", approve=lambda a: asked.append(a) or "yes")
    tool = next(t for t in ws.tools() if t.name == "remember")
    out = tool.invoke({"note": "Deploys go through   make release", "scope": "project"})
    assert out == "Saved to memory (this project): Deploys go through make release"
    assert asked[0]["tool"] == "remember" and "+- Deploys go through make release" in asked[0]["diff"]
    ws.approve = lambda a: "no"
    assert "declined" in tool.invoke({"note": "Something else"})
    assert "scope must be" in tool.invoke({"note": "x", "scope": "team"})
    assert [t for _, t in memory.listing(mem)][0].startswith("Deploys go through make release (")


def test_a_saved_note_reaches_the_next_answer(mem):
    ws = Workspace(mem, mode="yolo")
    call = AIMessage("", tool_calls=[{"name": "remember", "args": {"note": "Use tabs"}, "id": "r"}])
    s = Session(ToolFake(disable_streaming=True, messages=iter([call, AIMessage("Noted."), AIMessage("ok")])), ws)
    list(s.ask("remember that we use tabs"))
    first_key = s._agent_key
    list(s.ask("next"))
    assert s._agent_key != first_key  # the prompt was rebuilt with the new note
    assert "Use tabs" in prompts.agent_system_prompt(mem)


def test_long_notes_are_cut_and_the_prompt_keeps_the_newest(mem, monkeypatch):
    monkeypatch.setattr(memory, "MAX_SHOWN", 200)
    stored = memory.add(mem, "x" * 1000)
    assert len(stored) == memory.MAX_NOTE
    for i in range(20):
        memory.add(mem, f"note number {i}")
    section = memory.prompt_section(mem)
    assert "note number 19" in section and "note number 0 " not in section
