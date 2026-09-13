# -*- coding: utf-8 -*-
"""The test surface: an 80x40 grid of letters, scrollable, drawn with Cairo.

Nothing is cached into an ImageSurface. The obvious optimisation -- render the
whole grid once, blit it on every frame -- renders at logical size and comes out
soft on a HiDPI display, which for a task built on telling Ш from Щ is the one
thing that must not happen. Drawing directly hands Cairo the device scale. Only
the rows inside the clip are drawn, so a frame is about a thousand glyphs and
costs a couple of milliseconds; frames happen on click and on scroll, not on a
clock.

Appearance is configurable, which nothing else in this tree is. The reason is
that there is exactly one subject and he is also the only judge of whether a
field of 3200 uppercase letters is tiring to look at. That is not a question
data can settle, so the knobs exist and `preview` exists to turn them.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import grid  # noqa: E402
import store  # noqa: E402

RUN_SECONDS = 120

# Height the user settled on by measuring. Width is derived from the cell size.
WINDOW_HEIGHT = 724

PHASE_READY = "ready"
PHASE_RUNNING = "running"
PHASE_STOP = "stop"
PHASE_DONE = "done"
PHASE_PREVIEW = "preview"
PHASE_SUMMARY = "summary"

THEME_COLORS = os.path.expanduser(
    "~/.local/state/omarchy/current/theme/colors.toml"
)

# Used only when the theme file is unreadable -- a missing theme must not stop a
# run, and these are the retro-82 values the tool was designed against.
FALLBACK = {
    "dark_background": "#031222",
    "lighter_background": "#0a2540",
    "foreground": "#f6dcac",
    "light_foreground": "#a7c9c6",
    "dark_foreground": "#3f8f8a",
    "bright_foreground": "#f6dcac",
    "muted": "#2a6b78",
    "accent": "#faa968",
}

INK_CHOICES = (
    "foreground",
    "light_foreground",
    "dark_foreground",
    "bright_foreground",
)


@dataclass
class Appearance:
    # §2.1 asks for at least 24 px per cell. Everything else here is taste, and
    # taste is the user's.
    font_family: str = "JetBrainsMono Nerd Font"
    font_size: int = 20
    cell_w: int = 30
    cell_h: int = 34
    ink: str = "foreground"

    @classmethod
    def from_state(cls, state: Optional[Dict[str, str]] = None) -> "Appearance":
        state = state or store.load_state()
        got = cls()
        got.font_family = state.get("font_family", got.font_family)
        got.ink = state.get("ink", got.ink)
        for name in ("font_size", "cell_w", "cell_h"):
            try:
                setattr(got, name, int(state.get(name, getattr(got, name))))
            except (TypeError, ValueError):
                pass
        if got.ink not in INK_CHOICES:
            got.ink = "foreground"
        # A cell below the spec's floor is refused rather than clamped
        # silently: the run would still record, and the record would be
        # comparable with nothing.
        got.cell_w = max(24, got.cell_w)
        got.cell_h = max(24, got.cell_h)
        return got

    def describe(self) -> str:
        return (
            f"{self.font_family} {self.font_size}  "
            f"клетка {self.cell_w}x{self.cell_h}  чернила {self.ink}"
        )

    def as_state(self) -> Dict[str, str]:
        return {
            "font_family": self.font_family,
            "font_size": str(self.font_size),
            "cell_w": str(self.cell_w),
            "cell_h": str(self.cell_h),
            "ink": self.ink,
        }


def _hex_to_rgb(value: str) -> Tuple[float, float, float]:
    value = value.strip().strip('"').lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore


def load_palette() -> Dict[str, Tuple[float, float, float]]:
    """Follow the active Omarchy theme rather than hardcoding a look.

    Parsed by hand instead of with tomllib: the file is flat key = "value", and a
    real parser would turn a theme with any syntax quirk into a crash in a tool
    whose whole job is to open without friction.
    """
    raw = dict(FALLBACK)
    try:
        with open(THEME_COLORS, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                value = value.strip().strip('"')
                if key in FALLBACK and value.startswith("#") and len(value) == 7:
                    raw[key] = value
    except OSError:
        pass
    return {key: _hex_to_rgb(value) for key, value in raw.items()}


class Sheet(Gtk.DrawingArea):
    """The grid itself. Owns the marks; the window owns the phase."""

    def __init__(self, table: grid.Table, look: Appearance, palette, on_click) -> None:
        super().__init__()
        self.table = table
        self.look = look
        self.palette = palette
        self.on_click = on_click

        # (row, col) -> "left" | "right". First action wins: there is no undo,
        # and a second click on a marked cell must not quietly rewrite the first
        # one. §2.5 -- allowing corrections would turn the test into a measure
        # of tidiness.
        self.marks: Dict[Tuple[int, int], str] = {}
        self.stop_cell: Optional[Tuple[int, int]] = None
        self.locked = False

        self.set_content_width(grid.COLS * look.cell_w)
        self.set_content_height(grid.ROWS * look.cell_h)
        self.set_draw_func(self._draw)

        click = Gtk.GestureClick()
        # 0 means every button. Button 3 has to arrive here rather than opening
        # anything of its own; a DrawingArea has no context menu, so there is
        # nothing to suppress, but this is the line that would break if one were
        # ever added.
        click.set_button(0)
        click.connect("pressed", self._pressed)
        self.add_controller(click)

    # ----------------------------------------------------------- interaction

    def _pressed(self, gesture, n_press, x, y) -> None:
        col = int(x // self.look.cell_w)
        row = int(y // self.look.cell_h)
        if not (0 <= row < grid.ROWS and 0 <= col < grid.COLS):
            return
        button = {1: "left", 3: "right"}.get(gesture.get_current_button())
        if button is None:
            return
        self.on_click(row, col, button)

    def apply_mark(self, row: int, col: int, button: str) -> None:
        # Column 0 is the instruction letter. The click is logged -- the analysis
        # module counts them as `I` -- but it leaves no mark, because the letter
        # is not part of the row's scorable span.
        if col == 0:
            return
        self.marks.setdefault((row, col), button)
        self.queue_draw()

    def set_stop(self, row: int, col: int) -> None:
        self.stop_cell = (row, col)
        self.queue_draw()

    # -------------------------------------------------------------- drawing

    def _draw(self, area, cr, width, height) -> None:
        look = self.look
        cell_w, cell_h = look.cell_w, look.cell_h

        bg = self.palette["dark_background"]
        cr.set_source_rgb(*bg)
        cr.paint()

        x0, y0, x1, y1 = cr.clip_extents()
        first = max(0, int(y0 // cell_h))
        last = min(grid.ROWS - 1, int(y1 // cell_h))

        cr.select_font_face(look.font_family)
        cr.set_font_size(look.font_size)

        # Every glyph is centred on measured extents rather than on the advance
        # width. Eight letters means eight measurements, taken once per frame.
        offsets = {}
        for ch in grid.ALPHABET:
            ext = cr.text_extents(ch)
            offsets[ch] = (
                (cell_w - ext.width) / 2 - ext.x_bearing,
                (cell_h - ext.height) / 2 - ext.y_bearing,
            )

        instruction_bg = self.palette["lighter_background"]
        fg = self.palette.get(look.ink, self.palette["foreground"])
        mark = self.palette["muted"]
        accent = self.palette["accent"]

        for row in range(first, last + 1):
            top = row * cell_h
            letters = self.table.letters[row]

            # §2.2: a weak wash, never a bright fill. A saturated cell would give
            # the eye an anchor to jump between and change how the row is
            # scanned, which is the thing being measured.
            cr.set_source_rgb(*instruction_bg)
            cr.rectangle(0, top, cell_w, cell_h)
            cr.fill()

            cr.set_source_rgb(*fg)
            for col in range(grid.COLS):
                ch = letters[col]
                dx, dy = offsets[ch]
                cr.move_to(col * cell_w + dx, top + dy)
                cr.show_text(ch)

            for col in range(grid.COLS):
                button = self.marks.get((row, col))
                if button is None:
                    continue
                left = col * cell_w
                cr.set_source_rgb(*mark)
                cr.set_line_width(2.0)
                if button == "left":
                    # A diagonal, not a horizontal bar. A horizontal strike sits
                    # at the same height in every cell, so a run of them reads as
                    # one continuous rule and the eye stops resolving where one
                    # mark ends and the next begins. A slash keeps each mark its
                    # own object, and it is what the paper version uses.
                    cr.move_to(left + 4, top + cell_h - 4)
                    cr.line_to(left + cell_w - 4, top + 4)
                else:
                    # Underlined, clear of the descenders.
                    cr.move_to(left + 4, top + cell_h - 5)
                    cr.line_to(left + cell_w - 4, top + cell_h - 5)
                cr.stroke()

            if self.stop_cell and self.stop_cell[0] == row:
                col = self.stop_cell[1]
                cr.set_source_rgb(*accent)
                cr.set_line_width(2.0)
                cr.rectangle(col * cell_w + 1, top + 1, cell_w - 2, cell_h - 2)
                cr.stroke()

        if self.locked:
            # A veil rather than a blank: the marks stay visible, so the stop
            # click can be placed against what was actually worked through.
            cr.set_source_rgba(*bg, 0.55)
            cr.rectangle(x0, y0, x1 - x0, y1 - y0)
            cr.fill()


class TestWindow(Gtk.ApplicationWindow):
    """One run. With `preview` set, the same surface with no clock and no record.

    The preview shares this class rather than getting its own so that what is
    being judged is exactly what a run will look like -- a separate rendering
    path would eventually drift from the real one and make the comparison a lie.
    """

    def __init__(
        self,
        app,
        session: Optional[store.Session],
        look: Appearance,
        on_finish,
        preview: bool = False,
    ) -> None:
        super().__init__(application=app, title="Тест внимания")
        self.session = session
        self.look = look
        self.on_finish = on_finish
        self.preview = preview
        self.palette = load_palette()
        self.phase = PHASE_PREVIEW if preview else PHASE_READY
        self.t0: Optional[float] = None
        self.timeout_id: Optional[int] = None

        # Sized to the grid, not to the screen. Fullscreen was tried first and
        # the user measured a better one: at 40 columns of 30 px the content is
        # 1200 px, and a window of 1209 clears it exactly with no horizontal
        # scrollbar. The width is computed rather than pinned so that changing
        # the cell size does not silently reintroduce a horizontal scroll -- and
        # a horizontal scroll would matter, because the stop click has to land
        # on a cell that was actually on screen.
        self.set_default_size(grid.COLS * look.cell_w + 9, WINDOW_HEIGHT)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(box)

        self.header = Gtk.Label(xalign=0.0)
        self.header.set_margin_start(12)
        self.header.set_margin_end(12)
        self.header.set_margin_top(8)
        self.header.set_markup(
            "<b>Вычёркивайте</b> букву, с которой начинается строка — "
            "<b>левая кнопка</b>.  "
            "Если слева от неё стоит <b>А</b> — <b>подчёркивайте</b>, "
            "<b>правая кнопка</b>.  "
            "Отмену сделать нельзя."
        )
        box.append(self.header)

        table = session.table if session is not None else grid.generate(0)
        self.box = box
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.sheet = Sheet(table, look, self.palette, self._clicked)
        self.scroller.set_child(self.sheet)
        box.append(self.scroller)

        self.footer = Gtk.Label(xalign=0.0)
        self.footer.set_margin_start(12)
        self.footer.set_margin_end(12)
        self.footer.set_margin_bottom(8)
        box.append(self.footer)

        if preview:
            self._say(f"Предпросмотр — ничего не записывается.  {look.describe()}"
                      "   ·   Esc закрыть")
        else:
            self._say(
                "Отсчёт пойдёт с первого клика. "
                "Две минуты, времени на экране не будет."
            )

        # No countdown anywhere on screen during a run, on purpose: a visible
        # clock changes pacing, and pacing is part of what the run reveals.

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        # Same reasoning as Esc below, for the compositor's close binding: once
        # the two minutes are spent, the window does not go away until the stop
        # click has been placed.
        self.connect("close-request", self._close_request)

    def _say(self, text: str) -> None:
        self.footer.set_text(text)

    def _key(self, controller, keyval, keycode, state) -> bool:
        if keyval != 0xFF1B:  # Escape
            return False

        # Esc abandons a run in progress. Nothing is written -- a run walked out
        # of is not data, and saving a half-run would poison the statistics the
        # tool exists to build.
        #
        # Once the two minutes are up it stops working. At that point the run is
        # complete and the only thing still missing is the stop click, so an
        # accidental Esc would throw away real data to save one click. The stop
        # click is mandatory by design (§2.6): with random target density the
        # last mark can sit well short of where reading actually stopped, so
        # without it N is simply unknown.
        if self.phase == PHASE_SUMMARY:
            self.close()
            return True

        if self.phase in (PHASE_STOP, PHASE_DONE):
            self._say(
                "Прогон уже отработан. Отметьте клетку, на которой остановились — "
                "без неё не с чем сопоставить просмотренное."
            )
            return True

        self._cancel_timeout()
        self.on_finish(None)
        self.close()
        return True

    def _close_request(self, window) -> bool:
        if self.phase == PHASE_STOP:
            self._say(
                "Сначала отметьте клетку, на которой остановились. "
                "Прогон уже отработан, терять его не за что."
            )
            return True  # refuse the close
        return False

    def _cancel_timeout(self) -> None:
        if self.timeout_id is not None:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = None

    def _clicked(self, row: int, col: int, button: str) -> None:
        # In preview the marks are live -- the diagonal and the underline are
        # part of what is being judged -- but nothing is timed or recorded.
        if self.phase == PHASE_PREVIEW:
            self.sheet.apply_mark(row, col, button)
            return

        if self.phase == PHASE_DONE:
            return

        if self.phase == PHASE_STOP:
            self.sheet.set_stop(row, col)
            self.session.stop_click = {"row": row, "col": col}
            self.phase = PHASE_DONE
            self._say("Готово.")
            # A beat before the window goes, so the marked stop cell is actually
            # seen. Without it the screen simply vanishes and the click feels
            # unacknowledged.
            GLib.timeout_add(450, self._finish)
            return

        if self.phase == PHASE_READY:
            self.phase = PHASE_RUNNING
            self.t0 = time.monotonic()
            self.timeout_id = GLib.timeout_add(RUN_SECONDS * 1000, self._expired)
            self._say("Идёт.")

        t_ms = int((time.monotonic() - self.t0) * 1000)
        self.session.add_event(t_ms, row, col, button)
        self.sheet.apply_mark(row, col, button)

    def _expired(self) -> bool:
        self.timeout_id = None
        self.phase = PHASE_STOP
        self.sheet.locked = True
        self.sheet.queue_draw()
        self._say(
            "Время вышло. Обязательный шаг: отметьте клетку, на которой "
            "остановились — она задаёт границу просмотренного."
        )
        return False

    def _finish(self) -> bool:
        # on_finish writes the record and hands back the summary. Showing it in
        # THIS window rather than in a new one is the whole point: the test is
        # normally launched by the timer, not from a terminal, so anything
        # printed on stdout is unread and a notification is gone in seconds.
        # This window is already open and already focused, and reusing it adds
        # no dismissal that was not already there.
        summary = self.on_finish(self.session)
        if not summary:
            self.close()
            return False

        self.phase = PHASE_SUMMARY
        self.box.remove(self.scroller)

        panel = Gtk.Label(xalign=0.0, yalign=0.5, vexpand=True)
        panel.add_css_class("monospace")
        panel.set_text(summary)
        panel.set_margin_start(24)
        panel.set_margin_end(24)
        self.box.insert_child_after(panel, self.header)

        self.header.set_markup("<b>Прогон записан.</b>")
        self._say("Esc или закрыть окно.")
        return False
