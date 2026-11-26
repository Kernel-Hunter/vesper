"""Color themes for the terminal app, named after the desktop app's themes.

A theme is a handful of colors. `ui.apply_theme` turns one into the colors every screen
uses; `/theme` in the terminal picks one and saves it in `config.json` as `theme`.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    label: str
    mood: str
    dark: bool
    # The wordmark runs through these three, left to right; `accent` is the main one.
    accent: str
    accent_2: str
    accent_3: str
    text: str
    muted: str
    faint: str
    ok: str
    warn: str
    err: str
    add_bg: str   # background of an added line in a diff
    del_bg: str   # background of a removed line


THEMES: dict[str, Palette] = {p.name: p for p in (
    Palette("dusk", "Dusk", "indigo sky, one bright star", True,
            "#a594ff", "#d68cf0", "#f2c46d", "#ecebf5", "#9a96b0", "#767388",
            "#5fd4a0", "#f2c46d", "#ff7b8a", "#16352a", "#3d1d26"),
    Palette("graphite", "Graphite", "neutral grey, no tint", True,
            "#c7d0dc", "#9aa4b2", "#e6eaf0", "#eceef2", "#9a9fa8", "#72757c",
            "#7fd0a4", "#e3c27a", "#f28b8b", "#1d3127", "#3a2023"),
    Palette("carbon", "Carbon", "black and white", True,
            "#ffffff", "#bdbdc4", "#ffffff", "#f4f4f6", "#8f8f96", "#75757b",
            "#9be3b4", "#f0d58a", "#ff8f8f", "#183024", "#381c1f"),
    Palette("ember", "Ember", "fireside orange", True,
            "#ff8a5c", "#ffb487", "#ffd9a8", "#f6ece6", "#a48370", "#867166",
            "#8fd39b", "#ffcf7a", "#ff7a7a", "#1f3423", "#43211c"),
    Palette("ocean", "Ocean", "sea glass", True,
            "#4fd1c5", "#73e0d0", "#8fd3ff", "#e6f4f4", "#7ea3a8", "#5d7a7e",
            "#6ee7a8", "#f2d27a", "#ff8c95", "#123329", "#3a1f27"),
    Palette("forest", "Forest", "moss and fern", True,
            "#7ed492", "#a4db9d", "#e3e8a0", "#eaf2ea", "#8fa695", "#657a6b",
            "#8fe0a0", "#e8d07a", "#ff8a80", "#183520", "#3b1f1f"),
    Palette("rose", "Rosewood", "velvet pink", True,
            "#ff8fc2", "#ffb2d3", "#ffd6b5", "#f7eaf0", "#ac879a", "#856f7a",
            "#86d9a8", "#f5cf86", "#ff7f8f", "#183224", "#45202c"),
    Palette("daylight", "Daylight", "for light terminals", False,
            "#6d4cd8", "#a23ca8", "#b86e00", "#1d1b26", "#5f5b70", "#7f7c8e",
            "#16794a", "#9a6200", "#c42b3b", "#dff3e6", "#fbe2e4"),
)}

DEFAULT = "dusk"


_asked: list = []  # the terminal's answer, asked once per process


def ask_terminal_background(timeout: float = 0.3) -> bool | None:
    """Asks the terminal for its background color (OSC 11, answered by Terminal.app, iTerm2,
    VS Code, kitty, WezTerm, GNOME and Windows Terminal). True for light, False for dark,
    None when it cannot tell. A device-attributes query follows, which every terminal
    answers, so a terminal that ignores OSC 11 costs no waiting and leaves no stray input."""
    if _asked:
        return _asked[0]
    _asked.append(None)
    if os.name == "nt" or os.environ.get("TERM", "") in ("", "dumb") or os.environ.get("VESPER_NO_BG_QUERY"):
        return None
    try:
        import select
        import termios
        import tty

        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except (ImportError, OSError):
        return None
    try:
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd, termios.TCSANOW)
        try:
            os.write(fd, b"\033]11;?\033\\\033[c")
            reply, end = b"", time.monotonic() + timeout
            while time.monotonic() < end:
                ready, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
                if not ready:
                    break
                reply += os.read(fd, 1024)
                if re.search(rb"\033\[\?[\d;]*c", reply):  # the device answer comes last
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
    except (OSError, termios.error):
        return None
    finally:
        os.close(fd)
    _asked[0] = background_from_reply(reply)
    return _asked[0]


def background_from_reply(reply: bytes) -> bool | None:
    """`rgb:RRRR/GGGG/BBBB` (1 to 4 hex digits each) from an OSC 11 answer: True when light."""
    m = re.search(rb"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})", reply)
    if not m:
        return None
    r, g, b = (int(x, 16) / (16 ** len(x) - 1) for x in m.groups())
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5


def background_is_light() -> bool | None:
    """What the terminal's background is: its own answer first, then COLORFGBG ("0;15" is
    dark text on white). None when neither says."""
    answer = ask_terminal_background()
    if answer is not None:
        return answer
    value = os.environ.get("COLORFGBG", "")
    try:
        bg = int(value.split(";")[-1])
    except ValueError:
        return None
    return bg in (7, 15) or 9 <= bg <= 14


def resolve(name: str | None) -> Palette:
    """The theme called `name`, or the default for this terminal. A theme made for the other
    kind of background is swapped for this one's default (dark text on a black terminal is
    unreadable); `swapped_from` then says which theme was asked for."""
    global swapped_from
    swapped_from = None
    light = background_is_light()
    if name and name in THEMES:
        theme = THEMES[name]
        if light is None or theme.dark != light:
            return theme
        swapped_from = theme
    return THEMES["daylight" if light else DEFAULT]


swapped_from: Palette | None = None
