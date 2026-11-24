"""A small list you move through with the arrow keys and filter by typing.

Used by /resume (and Ctrl+R) so that going back to an earlier conversation needs no numbers
or ids: type a few letters of its title, pick it, press Enter.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Item:
    label: str        # the main line: a conversation's title
    meta: str = ""    # dim text after it: when, where, how long
    search: str = ""  # extra text the filter matches


MAX_ROWS = 10


def matches(item: Item, query: str) -> bool:
    hay = f"{item.label} {item.meta} {item.search}".lower()
    return all(w in hay for w in query.lower().split())


def choose(items: list[Item], title: str = "Pick one") -> int | None:
    """The index of the chosen item, or None when the user pressed Esc or Ctrl+C."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

    from vesper import ui

    if not items:
        return None
    state = {"index": 0, "top": 0}
    query = Buffer()

    def shown() -> list[int]:
        return [i for i, it in enumerate(items) if matches(it, query.text)]

    def on_change(_buf) -> None:
        state["index"], state["top"] = 0, 0

    query.on_text_changed += on_change

    def header():
        return [(f"fg:{ui.TEXT} bold", f"  {title}"),
                (f"fg:{ui.FAINT}", "   ↑ ↓ move · enter open · esc cancel · type to search")]

    def rows():
        visible = shown()
        if not visible:
            return [(f"fg:{ui.MUTED}", "    nothing matches")]
        top, out = state["top"], []
        for pos in range(top, min(len(visible), top + MAX_ROWS)):
            it = items[visible[pos]]
            on = pos == state["index"]
            label = it.label if len(it.label) <= 56 else it.label[:55] + "…"
            out += [(f"fg:{ui.ACCENT} bold", "  ❯ ") if on else ("", "    "),
                    (f"fg:{ui.TEXT} bold" if on else f"fg:{ui.TEXT}", label.ljust(58)),
                    (f"fg:{ui.MUTED}" if on else f"fg:{ui.FAINT}", it.meta + "\n")]
        if len(visible) > MAX_ROWS:
            out.append((f"fg:{ui.FAINT}", f"    {len(visible)} in all, keep moving or type to search"))
        return out

    def move(by: int) -> None:
        n = len(shown())
        if not n:
            return
        state["index"] = (state["index"] + by) % n
        if state["index"] < state["top"]:
            state["top"] = state["index"]
        elif state["index"] >= state["top"] + MAX_ROWS:
            state["top"] = state["index"] - MAX_ROWS + 1

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(event):
        move(-1)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("tab")
    def _(event):
        move(1)

    @kb.add("enter")
    def _(event):
        visible = shown()
        event.app.exit(result=visible[state["index"]] if visible else None)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("c-d")
    def _(event):
        event.app.exit(result=None)

    layout = Layout(HSplit([
        Window(FormattedTextControl(header), height=1),
        VSplit([Window(FormattedTextControl(lambda: [(f"fg:{ui.ACCENT} bold", "  / ")]), width=4, height=1),
                Window(BufferControl(buffer=query), height=1)]),
        Window(FormattedTextControl(rows), height=min(len(items), MAX_ROWS) + 1),
    ]), focused_element=query)
    app = Application(layout=layout, key_bindings=kb, full_screen=False, erase_when_done=True)
    try:
        return app.run()
    except (KeyboardInterrupt, EOFError):
        return None
