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
    here = settings_folder(root)
    for folder in [here, *here.parents]:
        if str(folder) in saved:
            return bool(saved[str(folder)])
    return False


def set_enabled(root: str | Path, on: bool) -> None:
    saved = onboarding.load_config().get("auto_commit")
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[str(settings_folder(root))] = bool(on)
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


# ---------------------------------------------------------------- a separate copy per chat
# A worktree is a second checkout of the same repository on its own branch, so a chat can
# change files without touching the main checkout or another chat's work. Vesper keeps them
# in ~/.vesper/worktrees/<repo>-<id>/<name>, on branches named vesper/<name>.
ENV_HINTS = (".venv", "venv", "node_modules", ".env", ".env.local")


def worktrees_home() -> Path:
    return onboarding.home() / "worktrees"


def settings_folder(path: str | Path) -> Path:
    """Where per-folder settings (/check, /git auto, trust) live for `path`: inside one of
    Vesper's worktrees that is the same place in the main checkout. Reads the worktree's
    `.git` file only; no git process."""
    p = Path(path).expanduser().resolve()
    home = worktrees_home().resolve()
    try:
        rel = p.relative_to(home)
    except ValueError:
        return p
    if len(rel.parts) < 2:
        return p
    wt = home / rel.parts[0] / rel.parts[1]
    try:
        line = (wt / ".git").read_text().strip()
    except OSError:
        return p
    if not line.startswith("gitdir:"):
        return p
    gitdir = Path(line.split(":", 1)[1].strip())
    main = gitdir.parents[2] if gitdir.parent.name == "worktrees" else None  # <main>/.git/worktrees/<name>
    return (main / Path(*rel.parts[2:])) if main else p


def _slug(text: str) -> str:
    import re

    word = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return word or "chat"


def start_worktree(root: str | Path, name: str = "") -> tuple[Path | None, str, list[str]]:
    """A new worktree of the repository holding `root`, on a new branch from HEAD.
    Returns (folder to work in, branch, notes for the user) or (None, "", [why not])."""
    import hashlib
    import time

    repo = repo_root(root)
    if repo is None:
        return None, "", ["This folder is not in a git repository."]
    if git(repo, "rev-parse", "-q", "--verify", "HEAD")[0] != 0:
        return None, "", ["The repository has no commit yet; make one first."]
    code, base = git(repo, "branch", "--show-current")
    base = base.strip() if code == 0 else ""
    slug = _slug(name or time.strftime("chat-%m%d-%H%M%S"))
    bucket = worktrees_home() / f"{repo.name}-{hashlib.sha1(str(repo).encode()).hexdigest()[:6]}"
    branch, path, n = f"vesper/{slug}", bucket / slug, 1
    while path.exists() or git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}")[0] == 0:
        n += 1
        branch, path = f"vesper/{slug}-{n}", bucket / f"{slug}-{n}"
    bucket.mkdir(parents=True, exist_ok=True)
    code, out = git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD", timeout=120)
    if code != 0:
        return None, "", [out or "git worktree add failed"]
    if base:
        git(repo, "config", "--local", f"branch.{branch}.vesperBase", base)
    notes = []
    if any(True for _ in _tracked_changes(repo)):
        notes.append("Uncommitted changes in the main checkout are not in this copy; it starts from the last commit.")
    missing = [h for h in ENV_HINTS if (repo / h).exists() and not (path / h).exists()]
    if missing:
        notes.append("Ignored files are not copied, so this copy has no " + ", ".join(missing)
                     + ". Install dependencies here (or point the check at the main checkout) before running tests.")
    try:
        sub = Path(root).expanduser().resolve().relative_to(repo)
    except ValueError:
        sub = Path()
    return (path / sub if (path / sub).is_dir() else path), branch, notes


def _tracked_changes(repo: Path) -> list[str]:
    code, out = git(repo, "status", "--porcelain=v1", "--untracked-files=no", raw=True)
    return [line[3:] for line in out.splitlines() if len(line) > 3] if code == 0 else []


def worktree_of(path: str | Path) -> dict | None:
    """{"path", "main", "branch", "base"} when `path` is inside one of Vesper's worktrees."""
    main_here = settings_folder(path)
    p = Path(path).expanduser().resolve()
    if main_here == p:
        return None
    top = repo_root(p)
    main = repo_root(main_here) if main_here.exists() else None
    if top is None or main is None:
        return None
    code, branch = git(top, "branch", "--show-current")
    branch = branch.strip()
    base = git(main, "config", "--local", f"branch.{branch}.vesperBase")[1].strip() if branch else ""
    return {"path": top, "main": main, "branch": branch, "base": base}


def save_work(info: dict, message: str) -> str | None:
    """Commits everything changed in the worktree (it is Vesper's own copy). Returns the sha or None."""
    wt = info["path"]
    if not git(wt, "status", "--porcelain", raw=True)[1].strip():
        return None
    git(wt, "add", "-A")
    code, out = git(wt, "commit", "-q", "-m", message, timeout=120)
    if code != 0:
        git(wt, "reset", "-q")
        raise RuntimeError(out.splitlines()[-1] if out else "git commit failed")
    return git(wt, "rev-parse", "--short", "HEAD")[1].strip()


def merge_worktree(info: dict, message: str) -> tuple[bool, str]:
    """Brings the worktree's branch into the main checkout and removes the worktree.
    Only when it is clearly safe; never forces. Returns (merged, what happened)."""
    main, wt, branch, base = info["main"], info["path"], info["branch"], info["base"]
    if (why := blocked(main)) is not None:
        return False, f"The main checkout cannot merge now: {why}."
    current = git(main, "branch", "--show-current")[1].strip()
    if base and current != base:
        return False, f"The main checkout is on {current}, not {base} where this copy started. Switch back first."
    if _tracked_changes(main):
        return False, ("The main checkout has uncommitted changes (" + ", ".join(_tracked_changes(main)[:4])
                       + "). Commit or stash them, then merge again.")
    try:
        save_work(info, message)
    except RuntimeError as exc:
        return False, f"Could not commit this copy's last changes: {exc}."
    ahead = git(main, "rev-list", "--count", f"{current}..{branch}")[1].strip()
    if ahead == "0":
        remove_worktree(info, force=False)
        return True, f"{branch} had nothing new; the copy is removed."
    code, out = git(main, "merge", "--no-ff", "-q", "-m", f"Merge {branch}", branch, timeout=120)
    if code != 0:
        conflicted = git(main, "diff", "--name-only", "--diff-filter=U")[1].split()
        git(main, "merge", "--abort")
        if conflicted:
            return False, ("These files changed in both places: " + ", ".join(conflicted[:6])
                           + ". Nothing was merged; the copy and its branch are kept.")
        return False, f"git merge failed: {out.splitlines()[-1] if out else 'unknown error'}. Nothing was merged."
    remove_worktree(info, force=False)
    return True, f"Merged {ahead} commit{'s' if ahead != '1' else ''} from {branch} into {current or 'HEAD'}; the copy is removed."


def discard_counts(info: dict) -> tuple[int, int]:
    """(uncommitted files, commits not in the base branch): what discarding would lose."""
    dirty_files = len([line for line in git(info["path"], "status", "--porcelain", raw=True)[1].splitlines() if line.strip()])
    base = info["base"] or "HEAD"
    code, n = git(info["main"], "rev-list", "--count", f"{base}..{info['branch']}")
    return dirty_files, int(n) if code == 0 and n.strip().isdigit() else 0


def remove_worktree(info: dict, force: bool) -> None:
    main = info["main"]
    git(main, "worktree", "remove", *(["--force"] if force else []), str(info["path"]), timeout=60)
    git(main, "worktree", "prune")
    git(main, "branch", "-D" if force else "-d", info["branch"])
    git(main, "config", "--local", "--remove-section", f"branch.{info['branch']}")
