"""The review pane: every file a chat changed, compared with what it held before, one file at a time."""

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper.coder import Session
from vesper.store import Store
from vesper.workspace import Workspace


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, call_id, **args):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])


def test_changes_lists_each_file_once_against_its_first_copy_and_reverts_one(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("a = 1\nb = 2\n")
    (proj / "logo.bin").write_bytes(b"\x00\x01")
    store = Store(tmp_path / "db" / "v.db")
    s = Session(ToolFake(disable_streaming=True, messages=iter([
        call("edit_file", "e1", path="app.py", old_string="a = 1", new_string="a = 10"),
        call("write_file", "w1", path="new.txt", content="hello\n"),
        AIMessage("first turn"),
        call("edit_file", "e2", path="app.py", old_string="b = 2", new_string="b = 20\nc = 3"),
        AIMessage("second turn"),
    ])), Workspace(proj, mode="yolo"), store)
    list(s.ask("one"))
    list(s.ask("two"))
    (proj / "logo.bin").write_bytes(b"\x00\x02")  # not changed by the chat: not listed
    files = {f["path"].replace("\\", "/").split("/")[-1]: f for f in s.changes()}
    assert set(files) == {"app.py", "new.txt"}
    app = files["app.py"]
    assert app["status"] == "modified" and (app["add"], app["del"]) == (3, 2)  # both turns, against the first copy
    assert "-a = 1" in app["diff"] and "+c = 3" in app["diff"]
    assert files["new.txt"]["status"] == "added" and files["new.txt"]["add"] == 1
    assert s.revert_file(app["path"]) is True
    assert (proj / "app.py").read_text() == "a = 1\nb = 2\n"
    assert s.revert_file(files["new.txt"]["path"]) is True and not (proj / "new.txt").exists()
    assert s.changes() == [] and s.revert_file(app["path"]) is False
    assert s._undone  # the model hears about it with the next message
    store.close()


def test_changes_api_and_revert(client, tmp_path):
    r = client.get("/api/chats/c9/changes")
    assert r.status_code == 200 and r.json() == {"files": []}
    assert client.get("/api/chats/bad id/changes").status_code in (400, 404)
    assert client.post("/api/chats/c9/changes/revert", json={}).status_code == 400
    r = client.post("/api/chats/c9/changes/revert", json={"path": str(tmp_path / "nope.txt")})
    assert r.status_code in (404, 409)
