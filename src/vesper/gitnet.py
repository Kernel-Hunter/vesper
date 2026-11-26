"""Git safety net: a commit after each turn that changed files, when the user asks for it.

`/git auto on` turns it on for the working folder (saved in `~/.vesper/config.json` under
"auto_commit", keyed by folder, like `/check`). After a turn that changed files, Vesper
commits exactly the files its tools wrote, with the user's own git identity and hooks.
Files that already had uncommitted changes before the turn are left out, so the user's
unfinished work never ends up in Vesper's commit. `/undo` takes the commit back when it is
still the latest one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vesper import onboarding

TIMEOUT_S = 20


def _key(root: str | Path) -> str:
    return str(Path(root).expanduser().resolve())


def enabled(root: str | Path) -> bool:
    """Whether auto-commit is on for `root` or the nearest parent folder that has a setting."""
    saved = onboarding.load_config().get("auto_commit")
    if not isinstance(saved, dict):
        return False
    here = Path(_key(root))
    for folder in [here, *here.parents]:
        if str(folder) in saved:
            return bool(saved[str(folder)])
    return False


def set_enabled(root: str | Path, on: bool) -> None:
    saved = onboarding.load_config().get("auto_commit")
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[_key(root)] = bool(on)
    onboarding.save_config({"auto_commit": saved})


def git(cwd: str | Path, *args: str, timeout: float = TIMEOUT_S, raw: bool = False) -> tuple[int, str]:
    """Runs git; (exit code, output). Never raises: no git, a timeout or a bad folder give 127, 124 or 1."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, errors="replace")
    except FileNotFoundError:
        return 127, "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} took longer than {timeout:.0f} s"
    except OSError as exc:
        return 1, str(exc)
    if raw:  # exactly what git printed (status lines start with a space)
        return proc.returncode, proc.stdout
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def repo_root(path: str | Path) -> Path | None:
    code, out = git(path, "rev-parse", "--show-toplevel")
    return Path(out.splitlines()[-1]).resolve() if code == 0 and out else None


def dirty(repo: Path) -> set[str]:
    """Absolute paths with uncommitted changes (staged, unstaged or untracked)."""
    code, out = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", raw=True)
    if code != 0:
        return set()
    paths, parts, i = set(), out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            paths.add(str((repo / entry[3:]).resolve()))
            if entry[0] in "RC":  # a rename carries the old path next
                i += 1
        i += 1
    return paths


def blocked(repo: Path) -> str | None:
    """Why a commit should not be made now, or None."""
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")):
        code, out = git(repo, "rev-parse", "--git-path", name)
        if code == 0 and out and (repo / out).exists():
            return f"{what} is in progress"
    if git(repo, "symbolic-ref", "-q", "HEAD")[0] != 0:
        return "HEAD is detached (no branch is checked out)"
    for key in ("user.name", "user.email"):
        code, out = git(repo, "config", key)
        if code != 0 or not out.strip():
            return f"git has no {key} set (git config --global {key} ...)"
    return None


def _relative(repo: Path, files: list[str]) -> list[str]:
    rel = []
    for f in files:
        try:
            rel.append(Path(f).resolve().relative_to(repo).as_posix())
        except ValueError:
            continue
    return rel


def commit(repo: Path, files: list[str], message: str) -> tuple[str | None, str]:
    """Commits exactly `files` (absolute paths inside `repo`). Returns (full sha, "") or (None, why)."""
    rel = _relative(repo, files)
    if rel:
        code, out = git(repo, "check-ignore", "--", *rel)
        ignored = set(out.splitlines()) if code == 0 else set()
        rel = [r for r in rel if r not in ignored]
    if not rel:
        return None, "nothing to commit"
    code, out = git(repo, "add", "-A", "--", *rel)
    if code != 0:
        return None, out or "git add failed"
    if git(repo, "diff", "--cached", "--quiet", "--", *rel)[0] == 0:
        return None, "nothing to commit"
    code, out = git(repo, "commit", "-q", "-m", message, "--", *rel, timeout=120)  # hooks may take a while
    if code != 0:
        git(repo, "reset", "-q", "--", *rel)  # leave the index as it was
        return None, out or "git commit failed"
    code, sha = git(repo, "rev-parse", "HEAD")
    return (sha.strip() if code == 0 else "?"), ""


def undo_commit(repo: Path, sha: str, files: list[str]) -> str:
    """Takes back Vesper's commit `sha` if it is still the latest; the files stay as they are
    (the caller puts them back). Returns what happened, for a note."""
    short = sha[:7]
    code, head = git(repo, "rev-parse", "HEAD")
    if code != 0 or head.strip() != sha:
        return f"Commit {short} is no longer the latest, so it stays; the files are back as they were."
    if git(repo, "rev-parse", "-q", "--verify", "HEAD~1")[0] != 0:
        return f"Commit {short} is the first in this repository, so it stays."
    if git(repo, "reset", "-q", "--soft", "HEAD~1")[0] != 0:
        return f"Could not take back commit {short}."
    rel = _relative(repo, files)
    if rel:
        git(repo, "reset", "-q", "--", *rel)
    return f"Commit {short} was taken back too."


def subject(text: str, limit: int = 68) -> str:
    """A commit subject from what the user asked: first line, tidied, cut at a word."""
    line = " ".join((text.strip().splitlines() or [""])[0].split())
    if not line:
        return "Update files"
    line = line[0].upper() + line[1:]
    if len(line) > limit:
        line = line[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return line
