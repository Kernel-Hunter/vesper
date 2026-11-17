"""The test-and-lint loop: the project's check command runs after edits and failures go back."""

import json
import sys

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from vesper import checks
from vesper import providers as prov
from vesper.coder import CHECK_KEY, Session, transcript
from vesper.store import Store
from vesper.workspace import Workspace


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def write(path, content, call_id):
    return AIMessage("", tool_calls=[{"name": "write_file", "args": {"path": path, "content": content}, "id": call_id}])


# Passes once fixed.txt exists; works the same on every OS.
CHECK = f'"{sys.executable}" -c "import os,sys; print(\'checking\'); sys.exit(0 if os.path.exists(\'fixed.txt\') else 1)"'


@pytest.fixture
def vhome(tmp_path, monkeypatch):
    monkeypatch.setattr(prov, "HOME", tmp_path / "vhome")
    return tmp_path / "vhome"


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def run(proj, *replies, mode="yolo"):
    store = Store(proj.parent / "db" / "v.db")
    s = Session(ToolFake(disable_streaming=True, messages=iter(replies)), Workspace(proj, mode=mode), store)
    events = list(s.ask("make it work"))
    return s, store, events


def test_set_find_and_remove_a_check_command(vhome, proj):
    assert checks.command_for(proj) == ""
    assert checks.set_command(proj, " pytest -q ") == "pytest -q"
    (proj / "sub").mkdir()
    assert checks.command_for(proj / "sub") == "pytest -q"  # a sub folder uses the project's
    assert json.loads((vhome / "config.json").read_text())["checks"] == {str(proj.resolve()): "pytest -q"}
    with pytest.raises(ValueError):
        checks.set_command(proj, "a\nb")
    checks.set_command(proj, "")
    assert checks.command_for(proj) == ""


def test_suggestions_come_from_the_project_files(proj):
    assert checks.suggest(proj) == ""
    (proj / "package.json").write_text('{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1", "lint": "eslint ."}}')
    assert checks.suggest(proj) == "npm run lint"
    (proj / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    assert checks.suggest(proj) == "npm test"
    (proj / "package.json").unlink()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n")
    (proj / "tests").mkdir()
    assert checks.suggest(proj) == "pytest -q"


def test_a_failed_check_goes_back_to_the_agent_until_it_passes(vhome, proj):
    checks.set_command(proj, CHECK)
    s, store, events = run(proj, write("a.txt", "one", "w1"), AIMessage("Done."),
                           write("fixed.txt", "ok", "w2"), AIMessage("Fixed it."))
    runs = [d for k, d in events if k == "tool_end" and d["name"] == "run_check"]
    assert [r["ok"] for r in runs] == [False, True]
    assert runs[0]["output"].startswith("exit code 1") and "checking" in runs[0]["output"]
    saved = store.load_messages(s.id)
    handed = [m for m in saved if isinstance(m, HumanMessage) and CHECK_KEY in m.additional_kwargs]
    assert len(handed) == 1 and "check command" in handed[0].content and "checking" in handed[0].content
    assert (proj / "fixed.txt").exists()
    assert len(s.user_turns()) == 1  # the check is not one of the user's messages
    turns = transcript(saved)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    steps = [p for p in turns[1]["parts"] if p["type"] == "tool"]
    assert [p["name"] for p in steps] == ["write_file", "run_check", "write_file"] and steps[1]["ok"] is False
    store.close()


def test_the_agent_gets_three_tries_then_vesper_stops(vhome, proj):
    checks.set_command(proj, CHECK)
    replies = []
    for i in range(4):
        replies += [write("a.txt", f"try {i}", f"w{i}"), AIMessage(f"Done {i}.")]
    s, store, events = run(proj, *replies)
    runs = [d for k, d in events if k == "tool_end" and d["name"] == "run_check"]
    assert len(runs) == checks.MAX_ROUNDS and not any(r["ok"] for r in runs)
    assert any(k == "notice" and "still fail" in d["text"] for k, d in events)
    store.close()


def test_no_check_without_edits(vhome, proj):
    checks.set_command(proj, CHECK)
    _s, store, events = run(proj, AIMessage("Nothing to change."))
    assert not any(d.get("name") == "run_check" for k, d in events if k == "tool_start")
    store.close()


def test_check_api_reads_and_sets_the_command(client, tmp_path):
    folder = tmp_path / "web"
    folder.mkdir()
    (folder / "go.mod").write_text("module x\n")
    r = client.get("/api/check", params={"folder": str(folder)})
    assert r.status_code == 200 and r.json()["command"] == "" and r.json()["suggestion"] == "go test ./..."
    r = client.post("/api/check", json={"folder": str(folder), "command": "go vet ./..."})
    assert r.json()["command"] == "go vet ./..."
    assert client.get("/api/check", params={"folder": str(folder)}).json()["command"] == "go vet ./..."
    assert client.post("/api/check", json={"folder": str(folder), "command": ""}).json()["command"] == ""
    assert client.get("/api/check", params={"folder": str(tmp_path / "missing")}).status_code == 400
