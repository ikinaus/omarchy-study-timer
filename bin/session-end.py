#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Asks whether the working day is over. Prints "yes" or "no", nothing else.

Exists because the day turns over at 08:00, an hour he is normally asleep
through, so the closing sound and notification -- the only ceremony this thing
has -- were being delivered to an empty room.

It closes nothing. The clock keeps running, work after the answer still accrues
to the same day, and 08:00 still does the bookkeeping. "Yes" only decides WHEN
the announcement is made.

The figures are on screen on purpose. A button with a pleasant sound behind it
is a small incentive to declare the day over early; showing what is worked and
what is left means the button is pressed with open eyes. For the same reason
neither button carries `suggested-action`: the styling would put a thumb on the
scale, and the whole value of the answer is that it is honest.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

# Untouched for this long and the answer is "no". A question nobody answered is
# not an ending, and the window must not sit there until morning.
PROMPT_TIMEOUT_SECONDS = 120


def hm(minutes: int) -> str:
    hours, rest = divmod(max(0, minutes), 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


class EndOfDayApp(Gtk.Application):
    """Two buttons and a deadline."""

    def __init__(self, worked: int, norm: int, asked: int, total: int) -> None:
        super().__init__(application_id="dev.mordrud.study.sessionend")
        self.worked = worked
        self.norm = norm
        self.asked = asked
        self.total = total
        self.choice = "no"
        self._timeout: Optional[int] = None

    def do_activate(self) -> None:
        window = Gtk.ApplicationWindow(application=self, title="Учебный таймер")
        window.set_default_size(460, -1)
        window.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(24)
        box.set_margin_end(24)
        window.set_child(box)

        title = Gtk.Label(xalign=0.0)
        title.set_markup("<b>Заканчиваем на сегодня?</b>")
        box.append(title)

        left = self.norm - self.worked
        body = Gtk.Label(xalign=0.0, wrap=True)
        if left > 0:
            body.set_text(f"Наработано {hm(self.worked)} из {hm(self.norm)}, "
                          f"до нормы ещё {hm(left)}.")
        else:
            body.set_text(f"Наработано {hm(self.worked)} — норма закрыта.")
        box.append(body)

        note = Gtk.Label(xalign=0.0, wrap=True)
        note.set_markup(
            "<small>«Да» — сыграет звук окончания дня и напоминания смолкнут "
            "до утра. День при этом не закрывается: вернётесь — время пойдёт "
            f"в те же сутки. Вопрос {self.asked} из {self.total}.</small>")
        box.append(note)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        box.append(buttons)

        for label, choice in (("Ещё поработаю", "no"),
                              ("Да, на сегодня всё", "yes")):
            button = Gtk.Button(label=label)
            button.connect("clicked", self._chose, choice, window)
            buttons.append(button)

        self._timeout = GLib.timeout_add_seconds(
            PROMPT_TIMEOUT_SECONDS, self._timed_out, window)
        window.present()

    def _chose(self, button, choice: str, window) -> None:
        self.choice = choice
        if self._timeout is not None:
            GLib.source_remove(self._timeout)
            self._timeout = None
        window.close()

    def _timed_out(self, window) -> bool:
        self._timeout = None
        self.choice = "no"
        window.close()
        return False


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--worked", type=int, default=0,
                        help="minutes banked today")
    parser.add_argument("--norm", type=int, default=600,
                        help="daily norm, minutes")
    parser.add_argument("--asked", type=int, default=1,
                        help="which ask this is, 1-based")
    parser.add_argument("--total", type=int, default=3,
                        help="asks allowed per day")
    args = parser.parse_args()

    app = EndOfDayApp(args.worked, args.norm, args.asked, args.total)
    app.run([sys.argv[0]])
    print(app.choice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
