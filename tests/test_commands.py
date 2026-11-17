"""Custom slash commands and skills: the file formats other harnesses use."""

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import commands, prompts
from vesper.coder import Session, transcript
from vesper.store import Store
from vesper.workspace import Workspace


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_front_matter_and_body():
    meta, body = commands.front_matter("---\ndescription: Review code\nargument-hint: '<file>'\n---\nDo $ARGUMENTS\n")
    assert meta == {"description": "Review code", "argument-hint": "<file>"} and body == "Do $ARGUMENTS\n"
    assert commands.front_matter("no front matter") == ({}, "no front matter")
    assert commands.front_matter("---\nunclosed: yes\nbody") == ({}, "---\nunclosed: yes\nbody")


def test_commands_from_project_folders_and_home_with_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("VESPER_HOME", str(tmp_path / "vhome"))
    proj = tmp_path / "proj"
    write(proj / ".vesper/commands/review.md", "---\ndescription: Review a file\nargument-hint: <file>\n---\nReview $ARGUMENTS now.\n")
    write(proj / ".claude/commands/git/commit.md", "Write a commit message for: $1 then $2.\n")
    write(proj / ".claude/commands/plain.md", "# Plain\nJust do it.\n")
    write(tmp_path / "vhome/commands/review.md", "the user's own review (the project's wins)\n")
    write(tmp_path / "vhome/commands/mine.md", "My command.\n")
    write(proj / ".vesper/commands/bad name.md", "skipped\n")
    (proj / "sub").mkdir()
    found = commands.find_commands(proj / "sub")  # found from a sub folder too
    assert set(found) == {"review", "git:commit", "plain", "mine"}
    assert found["review"]["description"] == "Review a file" and found["review"]["hint"] == "<file>"
    assert found["plain"]["description"] == "Plain" and found["mine"]["scope"] == "user"
    assert commands.expand(proj, "/review src/app.py") == "Review src/app.py now."
    assert commands.expand(proj, "/git:commit fix tests") == "Write a commit message for: fix then tests."
    assert commands.expand(proj, "/plain also this") == "# Plain\nJust do it.\n\nalso this"
    assert commands.expand(proj, "/nope") is None and commands.expand(proj, "hello") is None
    assert commands.expand(proj, "/review x", builtin={"/review"}) is None  # built-in commands win


def test_skills_are_listed_by_name_and_description_only(tmp_path, monkeypatch):
    monkeypatch.setenv("VESPER_HOME", str(tmp_path / "vhome"))
    proj = tmp_path / "proj"
    write(proj / ".agents/skills/pdf/SKILL.md", "---\nname: pdf\ndescription: Fill and merge PDF forms.\n---\n" + "Long body " * 50)
    write(proj / ".claude/skills/nodesc/SKILL.md", "---\nname: nodesc\n---\nNo description: skipped.\n")
    write(tmp_path / "vhome/skills/pdf/SKILL.md", "---\nname: pdf\ndescription: the user's copy, hidden\n---\n")
    write(tmp_path / "vhome/skills/charts/SKILL.md", "---\ndescription: Make charts.\n---\n")
    skills = commands.find_skills(proj)
    assert [(s["name"], s["scope"]) for s in skills] == [("pdf", "project"), ("charts", "user")]
    prompt = prompts.agent_system_prompt(proj)
    assert "# Skills" in prompt and "pdf: Fill and merge PDF forms." in prompt and "Long body" not in prompt
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("VESPER_HOME", str(tmp_path / "other-home"))
    assert "# Skills" not in prompts.agent_system_prompt(empty)  # no skills: no section, no tokens


class Fake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_a_command_turn_saves_what_the_user_typed(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    store = Store(tmp_path / "db" / "v.db")
    s = Session(Fake(disable_streaming=True, messages=iter([AIMessage("ok")])), Workspace(proj), store)
    list(s.ask("Explain notes.md simply.", shown="/explain notes.md"))
    saved = store.load_messages(s.id)
    assert saved[0].content == "Explain notes.md simply."  # the model got the command's text
    assert transcript(saved)[0]["text"] == "/explain notes.md"  # the chat shows what was typed
    assert s.rewind(0)["text"] == "/explain notes.md"
    store.close()


def test_terminal_runs_a_custom_command(tmp_path, monkeypatch):
    from vesper import tui

    monkeypatch.setenv("VESPER_HOME", str(tmp_path / "vhome"))
    proj = tmp_path / "proj"
    write(proj / ".vesper/commands/hello.md", "Say hello to $ARGUMENTS.\n")
    app = tui.App.__new__(tui.App)
    app.workspace = Workspace(proj)
    ran = []
    app.run_turn = lambda text, shown=None, images=None: ran.append((text, shown))
    app.expand_mentions = lambda text: (text, [])
    assert app.command("/hello world") is True
    assert ran == [("Say hello to world.", "/hello world")]
