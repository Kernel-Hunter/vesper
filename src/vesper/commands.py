"""Custom slash commands and skills, in the formats other harnesses use.

A command is a Markdown file; its name is the file name. `/review src/app.py` sends the
file's text with `$ARGUMENTS` replaced by `src/app.py` (and `$1`, `$2`... by single words):

    ---
    description: Review a file for bugs
    argument-hint: <file>
    ---
    Review $ARGUMENTS for bugs and explain each one.

Commands come from `.vesper/commands/` and `.claude/commands/` in the project (and its parent
folders up to home), then `~/.vesper/commands/`. Sub folders give names like `/git:commit`.

A skill is a folder with a `SKILL.md` file whose front matter has a name and a description.
Only the names and descriptions go into the system prompt; the model reads a skill's file
when a task needs it, so a hundred skills cost a few lines, not a hundred files. Skills come
from `.vesper/skills/`, `.claude/skills/` and `.agents/skills/` in the project, then
`~/.vesper/skills/` and `~/.agents/skills/`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

COMMAND_DIRS = (".vesper/commands", ".claude/commands")
SKILL_DIRS = (".vesper/skills", ".claude/skills", ".agents/skills")
USER_SKILL_DIRS = (".agents/skills",)      # under the home folder (shared with other tools)
MAX_FILE = 64_000
MAX_SKILLS = 60
MAX_COMMANDS = 200
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _vesper_home() -> Path:
    return Path(os.environ.get("VESPER_HOME") or Path.home() / ".vesper")


def front_matter(text: str) -> tuple[dict, str]:
    """({key: value}, body) for a file that may start with a `---` block of `key: value` lines."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t", "-")):
            meta[key.strip().lower()] = value.strip().strip("\"'")
    body = text[end + 4:]
    nl = body.find("\n")
    return meta, (body[nl + 1:] if nl >= 0 else "").lstrip("\n")


def _project_folders(root: str | Path) -> list[Path]:
    """The working folder and its parents, up to (not including) the home folder."""
    here, home = Path(root).resolve(), Path.home().resolve()
    out = []
    for folder in [here, *here.parents]:
        if folder == home:
            break
        out.append(folder)
    return out


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# -------------------------------------------------------------------- commands
def find_commands(root: str | Path) -> dict[str, dict]:
    """{name: {"name", "description", "hint", "path", "scope"}}; the nearest definition wins."""
    sources = [(f / d, "project") for f in _project_folders(root) for d in COMMAND_DIRS]
    sources.append((_vesper_home() / "commands", "user"))
    found: dict[str, dict] = {}
    for base, scope in sources:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md"))[:MAX_COMMANDS]:
            name = ":".join(path.relative_to(base).with_suffix("").parts)
            if not NAME.match(name) or name in found:
                continue
            text = _read(path)
            if text is None:
                continue
            meta, body = front_matter(text)
            first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
            found[name] = {"name": name, "description": (meta.get("description") or first)[:120],
                           "hint": meta.get("argument-hint", "")[:60], "path": str(path), "scope": scope}
    return found


def expand(root: str | Path, line: str, builtin=frozenset()) -> str | None:
    """The prompt a `/name args` line stands for, or None when it is not a custom command."""
    if not line.startswith("/"):
        return None
    word, _, args = line[1:].partition(" ")
    if not word or "/" + word.lower() in builtin:
        return None
    command = find_commands(root).get(word)
    if command is None:
        return None
    text = _read(Path(command["path"]))
    if text is None:
        return None
    _meta, body = front_matter(text)
    args = args.strip()
    words = args.split()
    if args and "$ARGUMENTS" not in body and not re.search(r"\$[1-9]", body):
        body = body.rstrip() + "\n\n" + args  # a command without placeholders still gets what was typed
    body = body.replace("$ARGUMENTS", args)
    body = re.sub(r"\$([1-9])", lambda m: words[int(m.group(1)) - 1] if int(m.group(1)) <= len(words) else "", body)
    return body.strip() or None


# -------------------------------------------------------------------- skills
def find_skills(root: str | Path) -> list[dict]:
    """[{"name", "description", "path", "scope"}] of every skill; the nearest of a name wins."""
    sources = [(f / d, "project") for f in _project_folders(root) for d in SKILL_DIRS]
    sources.append((_vesper_home() / "skills", "user"))
    sources += [(Path.home() / d, "user") for d in USER_SKILL_DIRS]
    seen, out = set(), []
    for base, scope in sources:
        if not base.is_dir():
            continue
        for skill_file in sorted(base.glob("*/SKILL.md")):
            text = _read(skill_file)
            if text is None:
                continue
            meta, _body = front_matter(text)
            name = (meta.get("name") or skill_file.parent.name).strip()
            description = " ".join((meta.get("description") or "").split())
            if not name or not description or name in seen:
                continue  # without a description the model cannot know when to use it
            seen.add(name)
            out.append({"name": name[:64], "description": description[:300], "path": str(skill_file), "scope": scope})
            if len(out) >= MAX_SKILLS:
                return out
    return out


def skills_prompt(root: str | Path) -> str:
    """The system prompt section that lists the skills, or "" when there are none."""
    try:
        skills = find_skills(root)
    except OSError:
        return ""
    if not skills:
        return ""
    lines = "\n".join(f"- {s['name']}: {s['description']} ({s['path']})" for s in skills)
    return ("\n\n# Skills\nWhen a task matches one of these skills, read its file with read_file first and "
            "follow it. Files next to it hold more detail; read them only when the skill says to.\n" + lines)
