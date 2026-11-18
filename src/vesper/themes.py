"""Color themes for the terminal app, named after the desktop app's themes.

A theme is a handful of colors. `ui.apply_theme` turns one into the colors every screen
uses; `/theme` in the terminal picks one and saves it in `config.json` as `theme`.
"""

from __future__ import annotations

import os
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
            "#a594ff", "#d68cf0", "#f2c46d", "#ecebf5", "#9a96b0", "#5d5a72",
            "#5fd4a0", "#f2c46d", "#ff7b8a", "#16352a", "#3d1d26"),
    Palette("graphite", "Graphite", "neutral grey, no tint", True,
            "#c7d0dc", "#9aa4b2", "#e6eaf0", "#eceef2", "#9a9fa8", "#5b5f66",
            "#7fd0a4", "#e3c27a", "#f28b8b", "#1d3127", "#3a2023"),
    Palette("carbon", "Carbon", "black and white", True,
            "#ffffff", "#bdbdc4", "#ffffff", "#f4f4f6", "#8f8f96", "#55555c",
            "#9be3b4", "#f0d58a", "#ff8f8f", "#183024", "#381c1f"),
    Palette("ember", "Ember", "fireside orange", True,
            "#ff8a5c", "#ffb487", "#ffd9a8", "#f6ece6", "#a48370", "#6a5246",
            "#8fd39b", "#ffcf7a", "#ff7a7a", "#1f3423", "#43211c"),
    Palette("ocean", "Ocean", "sea glass", True,
            "#4fd1c5", "#73e0d0", "#8fd3ff", "#e6f4f4", "#7ea3a8", "#4b6a6f",
            "#6ee7a8", "#f2d27a", "#ff8c95", "#123329", "#3a1f27"),
    Palette("forest", "Forest", "moss and fern", True,
            "#7ed492", "#a4db9d", "#e3e8a0", "#eaf2ea", "#8fa695", "#566b5c",
            "#8fe0a0", "#e8d07a", "#ff8a80", "#183520", "#3b1f1f"),
    Palette("rose", "Rosewood", "velvet pink", True,
            "#ff8fc2", "#ffb2d3", "#ffd6b5", "#f7eaf0", "#ac879a", "#6e5562",
            "#86d9a8", "#f5cf86", "#ff7f8f", "#183224", "#45202c"),
    Palette("daylight", "Daylight", "for light terminals", False,
            "#6d4cd8", "#a23ca8", "#b86e00", "#1d1b26", "#5f5b70", "#9b97ab",
            "#16794a", "#9a6200", "#c42b3b", "#dff3e6", "#fbe2e4"),
)}

DEFAULT = "dusk"


def background_is_light() -> bool:
    """What COLORFGBG says about the terminal's background ("0;15" is dark text on white).
    Most terminals do not set it; then we assume a dark background."""
    value = os.environ.get("COLORFGBG", "")
    try:
        bg = int(value.split(";")[-1])
    except ValueError:
        return False
    return bg in (7, 15) or 9 <= bg <= 14


def resolve(name: str | None) -> Palette:
    """The theme called `name`, or the default for this terminal."""
    if name and name in THEMES:
        return THEMES[name]
    return THEMES["daylight" if background_is_light() else DEFAULT]
