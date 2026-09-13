#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Entry point for the PVT.

Same shape as the cancellation test it replaces on the timer, deliberately:
`run` starts a measurement now, `prompt` shows the offer and reports back what
was chosen without doing anything about it. The bookkeeping -- thresholds, the
postpone ladder -- stays in timer.sh, which is the thing that ticks.

Anything printed on stdout is read by a shell script. One token per line.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import store  # noqa: E402
import ui  # noqa: E402

# Untouched for this long, the offer counts as a postpone at the current rung.
# Leaving it up forever is the surest way to make the instrument resented, and
# a resented instrument stops being run.
PROMPT_TIMEOUT_SECONDS = 120


def notify(title: str, body: str, urgency: str = "normal") -> None:
    try:
        subprocess.Popen(
            ["notify-send", "-u", urgency, title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        # No notification daemon is not a reason to lose a run.
        pass


def summary_for(session_id: str) -> Optional[str]:
    """The lines shown in the window once the run is over.

    Absent until analyze.py exists, and absent if it breaks. Neither costs the
    record: by the time this is called, the session is already on disk.
    """
    try:
        import analyze
    except ImportError:
        return None
    try:
        return analyze.after_run(session_id)
    except Exception:
        return None


class PvtApp(Gtk.Application):
    def __init__(self, session: store.Session) -> None:
        super().__init__(application_id="dev.mordrud.pvt")
        self.session = session
        self.result: Optional[store.Session] = None
        self.committed = False

    def do_activate(self) -> None:
        ui.PvtWindow(self, self.session, self._finished).present()

    def _finished(self, session: Optional[store.Session]) -> Optional[str]:
        """Called by the window when the five minutes are up, or with None when
        the run was abandoned.

        The record is written here rather than after the main loop returns,
        because the window wants the summary back to show in place -- and
        writing first means a broken summary cannot cost the run.
        """
        self.result = session
        if session is None:
            return None
        store.commit(session)
        self.committed = True
        return summary_for(session.session_id)


def run_test(trigger: str, block_index: int, minutes_worked: int) -> Optional[str]:
    session = store.begin(trigger, block_index, minutes_worked)
    app = PvtApp(session)
    app.run([sys.argv[0]])

    if app.result is None:
        return None
    if not app.committed:
        store.commit(app.result)

    summary = summary_for(app.result.session_id)
    if summary:
        # Not a duplicate of the panel: the panel goes away with the window,
        # and the notification history is where a run is found an hour later.
        urgency = "critical" if "прекратить" in summary else "normal"
        notify("Тест бдительности", summary, urgency)
    return app.result.session_id


class PromptApp(Gtk.Application):
    """Three buttons and a deadline."""

    def __init__(self, minutes_worked: int, postpone_minutes: int) -> None:
        super().__init__(application_id="dev.mordrud.pvt.prompt")
        self.minutes_worked = minutes_worked
        self.postpone_minutes = postpone_minutes
        self.choice = "postpone"
        self._timeout: Optional[int] = None

    def do_activate(self) -> None:
        window = Gtk.ApplicationWindow(application=self, title="Тест бдительности")
        window.set_default_size(480, -1)
        window.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(24)
        box.set_margin_end(24)
        window.set_child(box)

        hours, minutes = divmod(self.minutes_worked, 60)
        worked = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"

        title = Gtk.Label(xalign=0.0)
        title.set_markup(f"<b>Позади {worked} работы.</b>")
        box.append(title)

        body = Gtk.Label(xalign=0.0, wrap=True)
        body.set_text(f"{store.DURATION_S // 60} минут: жать пробел, "
                      f"как только в рамке побегут цифры.")
        box.append(body)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        box.append(buttons)

        for label, choice in (
            (f"Отложить на {self.postpone_minutes} мин", "postpone"),
            ("Пропустить", "skip"),
            ("Пройти", "run"),
        ):
            button = Gtk.Button(label=label)
            if choice == "run":
                button.add_css_class("suggested-action")
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
        self.choice = "postpone"
        window.close()
        return False


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "prompt", "mode", "status"])
    parser.add_argument("value", nargs="?", default="")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--block", type=int, default=0)
    parser.add_argument("--worked", type=int, default=0)
    parser.add_argument("--postpone-minutes", type=int, default=15)
    args = parser.parse_args()

    if args.command == "mode":
        if args.value not in ("training", "operational"):
            print("mode: training | operational", file=sys.stderr)
            return 1
        state = store.load_state()
        state["mode"] = args.value
        store.save_state(state)
        print(args.value)
        return 0

    if args.command == "status":
        state = store.load_state()
        for key in sorted(state):
            print(f"{key}={state[key]}")
        print(f"sessions={len(store.load_all())}")
        print(f"protocol={store.PROTOCOL_NAME} v{store.PROTOCOL_VERSION} "
              f"{store.DURATION_S}s isi={store.ISI_MIN_MS}-{store.ISI_MAX_MS}ms")
        return 0

    if args.command == "prompt":
        # Prints the choice and stops. A Gtk.Application registers once per
        # process, so running the test in the same process after the offer has
        # closed is not reliable -- and the branch belongs in timer.sh anyway,
        # which already owns the thresholds.
        app = PromptApp(args.worked, args.postpone_minutes)
        app.run([sys.argv[0]])
        print(app.choice)
        return 0

    session_id = run_test(args.trigger, args.block, args.worked)
    if session_id is None:
        print("aborted")
        return 1
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
