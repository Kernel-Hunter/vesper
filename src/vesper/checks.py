"""The project's check command: tests or a linter that run after the agent edits files.

`/check pytest -q` sets it for the working folder (saved in `~/.vesper/config.json` under
"checks", keyed by folder). When a turn changed files, the command runs once the agent says
it is done; if it fails, its output goes back to the agent, which fixes the cause and stops
again, up to MAX_ROUNDS times. The user typed the command, so it runs without an approval.
"""

from __future__ import annotations

import json
from pathlib import Path

from vesper import onboarding

MAX_ROUNDS = 3          # failed checks the agent gets to fix in one turn
TIMEOUT_S = 300
MAX_COMMAND = 500


def _key(root: str | Path) -> str:
    return str(Path(root).expanduser().resolve())


def command_for(root: str | Path) -> str:
    """The check command of `root` or of the nearest parent folder that has one ("" when none)."""
    saved = onboarding.load_config().get("checks")
    if not isinstance(saved, dict):
        return ""
    here = Path(_key(root))
    for folder in [here, *here.parents]:
        cmd = saved.get(str(folder))
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
    return ""


def set_command(root: str | Path, command: str | None) -> str:
    """Save (or with None / "" remove) the check command of `root`. Returns what is saved now."""
    command = (command or "").strip()
    if len(command) > MAX_COMMAND or "\n" in command:
        raise ValueError(f"A check command is one line of at most {MAX_COMMAND} characters.")
    saved = onboarding.load_config().get("checks")
    saved = dict(saved) if isinstance(saved, dict) else {}
    if command:
        saved[_key(root)] = command
    else:
        saved.pop(_key(root), None)
    onboarding.save_config({"checks": saved})
    return command


def suggest(root: str | Path) -> str:
    """A likely check command for the project in `root`, from its files ("" when unsure)."""
    root = Path(root)
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
        if isinstance(scripts, dict):
            test = scripts.get("test", "")
            if isinstance(test, str) and test and "no test specified" not in test:
                return "npm test"
            if "lint" in scripts:
                return "npm run lint"
    except (OSError, ValueError):
        pass
    if any((root / name).is_file() for name in ("pyproject.toml", "pytest.ini", "setup.cfg")) and (root / "tests").is_dir():
        return "pytest -q"
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Package.swift").is_file():
        return "swift build"
    try:
        if any(line.startswith("test:") for line in (root / "Makefile").read_text(encoding="utf-8").splitlines()):
            return "make test"
    except OSError:
        pass
    return ""


def failure_prompt(command: str, code: int | None, output: str) -> str:
    """What the agent reads when the check failed."""
    how = "did not finish in time" if code is None else f"exited with code {code}"
    return (f"The project's check command `{command}` {how} after your changes. Fix the cause "
            f"(do not weaken or skip the checks), then finish.\n\nOutput (the end):\n```\n{output.strip()}\n```")
