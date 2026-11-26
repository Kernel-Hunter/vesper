"""The git safety net: a commit per turn, only Vesper's files, and /undo taking it back."""

import subprocess
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import gitnet
from vesper.coder import Session
from vesper.store import Store
from vesper.workspace import Workspace

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")


def run(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=30).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # CI runners have no git identity: give this repository its own
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Ada"), ("config", "user.email", "ada@example.com"),
                 ("config", "commit.gpgsign", "false"), ("config", "core.hooksPath", str(tmp_path / "hooks"))):
        run(repo, *args)
    (repo / "a.py").write_text("x = 1\n")
    (repo / "mine.py").write_text("y = 1\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "start")
    return repo.resolve()


class Fake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def session_for(repo, tmp_path, replies):
    gitnet.set_enabled(repo, True)
    return Session(Fake(disable_streaming=True, messages=iter(replies)), Workspace(repo, mode="yolo"), store=Store(tmp_path / "v.db"))


def edit(path, old, new, cid="e1"):
    return AIMessage("", tool_calls=[{"name": "edit_file", "args": {"path": path, "old_string": old, "new_string": new},
                                      "id": cid}])


def test_a_turn_that_changes_files_is_committed_with_only_its_files(repo, tmp_path):
    (repo / "mine.py").write_text("y = 2  # the user's unfinished work\n")
    s = session_for(repo, tmp_path, [
        edit("a.py", "x = 1", "x = 2"),
        AIMessage("", tool_calls=[{"name": "write_file", "args": {"path": "new.py", "content": "z = 1\n"}, "id": "w1"}]),
        edit("mine.py", "y = 2", "y = 3", "e2"),
        AIMessage("Done."),
    ])
    events = dict((k, d) for k, d in s.ask("fix the counter in a.py\nmore detail here"))
    commit = events["commit"]
    assert commit["sha"] and commit["message"] == "Fix the counter in a.py"
    assert sorted(Path(f).name for f in commit["files"]) == ["a.py", "new.py"]
    assert [Path(f).name for f in commit["skipped"]] == ["mine.py"]
    assert run(repo, "log", "-1", "--format=%an <%ae> %s") == "Ada <ada@example.com> Fix the counter in a.py"
    assert run(repo, "show", "--name-only", "--format=", "HEAD").split() == ["a.py", "new.py"]
    assert "mine.py" in run(repo, "status", "--porcelain")  # still the user's, uncommitted


def test_undo_takes_the_commit_back_and_restores_the_files(repo, tmp_path):
    s = session_for(repo, tmp_path, [edit("a.py", "x = 1", "x = 2"), AIMessage("Done.")])
    events = dict((k, d) for k, d in s.ask("bump x"))
    assert events["commit"]["sha"] == run(repo, "rev-parse", "HEAD")
    assert s.undo()
    assert "taken back" in s.undo_note
    assert run(repo, "log", "-1", "--format=%s") == "start"
    assert (repo / "a.py").read_text() == "x = 1\n" and run(repo, "status", "--porcelain") == ""


def test_undo_keeps_a_commit_that_is_no_longer_the_latest(repo, tmp_path):
    s = session_for(repo, tmp_path, [edit("a.py", "x = 1", "x = 2"), AIMessage("Done.")])
    list(s.ask("bump x"))
    (repo / "other.txt").write_text("later\n")
    run(repo, "add", "other.txt")
    run(repo, "commit", "-q", "-m", "the user's own commit")
    s.undo()
    assert "no longer the latest" in s.undo_note
    assert run(repo, "log", "-1", "--format=%s") == "the user's own commit"


def test_nothing_happens_when_it_is_off_or_nothing_changed(repo, tmp_path):
    s = session_for(repo, tmp_path, [AIMessage("Just talking."), edit("a.py", "x = 1", "x = 2"), AIMessage("ok")])
    assert "commit" not in dict(s.ask("hello"))
    gitnet.set_enabled(repo, False)
    assert "commit" not in dict(s.ask("bump x"))
    assert run(repo, "log", "-1", "--format=%s") == "start"


def test_a_merge_in_progress_or_a_failing_hook_is_reported_not_forced(repo, tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed: a.py' >&2\nexit 1\n")
    hook.chmod(0o755)
    s = session_for(repo, tmp_path, [edit("a.py", "x = 1", "x = 2"), AIMessage("Done.")])
    commit = dict(s.ask("bump x"))["commit"]
    if subprocess.os.name != "nt":  # hooks run through sh on POSIX
        assert not commit["sha"] and "lint failed" in commit["error"]
        assert run(repo, "diff", "--cached", "--name-only") == ""  # the index is as it was
    hook.unlink()
    (Path(run(repo, "rev-parse", "--absolute-git-dir")) / "MERGE_HEAD").write_text(run(repo, "rev-parse", "HEAD") + "\n")
    assert gitnet.blocked(repo) == "a merge is in progress"


def test_a_detached_head_or_no_identity_is_refused(repo):
    assert gitnet.blocked(repo) is None
    run(repo, "checkout", "-q", "--detach")
    assert "detached" in gitnet.blocked(repo)


def test_subjects_are_tidy():
    assert gitnet.subject("  fix   the bug\nplease") == "Fix the bug"
    assert gitnet.subject("") == "Update files"
    long = gitnet.subject("add " + "word " * 40)
    assert len(long) <= 70 and long.endswith("…")


def test_the_setting_is_per_folder_and_inherited(tmp_path):
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert not gitnet.enabled(child)
    gitnet.set_enabled(tmp_path / "a", True)
    assert gitnet.enabled(child) and not gitnet.enabled(tmp_path)
    gitnet.set_enabled(child, False)
    assert not gitnet.enabled(child) and gitnet.enabled(tmp_path / "a")
