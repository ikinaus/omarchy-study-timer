#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the attention test.

Two ways in. `run` starts a test immediately -- that is `study attention`, and
the user reaching for it on his own is the second mode of use the spec calls for
(§1: checking whether a felt slump is real). `prompt` is what the timer calls
when a work threshold is crossed: it offers the test and reports back what was
chosen, so the decision to postpone or skip is the user's and the bookkeeping is
the timer's.

Anything printed on stdout is read by timer.sh. Keep it to one token per line.
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

# Untouched for this long, the offer counts as "postpone" at the current rung.
# The alternative -- leaving it up forever -- is the surest way to make the whole
# instrument something to be resented, which §1 rules out ahead of any
# methodological concern.
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
    """What is shown once a run is over.

    The numbers are shown in both modes; only the verdict is held back to
    operational (§5.1). Withholding the numbers too was considered and dropped:
    a test that reports nothing stops being run, and §1 puts that ahead of
    methodological tidiness. The mitigation is in which numbers -- speed and
    accuracy always appear together, so a deliberate push shows its own cost.
    """
    try:
        import analyze
    except ImportError:
        return None
    try:
        return analyze.after_run(session_id)
    except Exception:
        # The record is already on disk. A broken summary must never propagate
        # into the exit path and take the data with it.
        return None


class TestApp(Gtk.Application):
    """Runs one test window and reports the session that came out of it."""

    def __init__(
        self,
        session: Optional[store.Session],
        look: ui.Appearance,
        preview: bool = False,
    ) -> None:
        super().__init__(application_id="dev.mordrud.attention")
        self.session = session
        self.look = look
        self.preview = preview
        self.result: Optional[store.Session] = None
        self.committed = False

    def do_activate(self) -> None:
        window = ui.TestWindow(
            self, self.session, self.look, self._finished, preview=self.preview
        )
        window.present()

    def _finished(self, session: Optional[store.Session]) -> Optional[str]:
        """Called by the window once the stop click has landed.

        The record is written HERE, not after the main loop returns, because the
        window wants the summary back so it can show it in place. Writing first
        also means the run survives even if the summary blows up.
        """
        self.result = session
        if session is None:
            return None
        store.commit(session)
        self.committed = True
        return summary_for(session.session_id)


def appearance_from(args, state=None) -> ui.Appearance:
    """State first, then whatever the command line overrides."""
    look = ui.Appearance.from_state(state)
    if args.font:
        look.font_family = args.font
    if args.size:
        look.font_size = args.size
    if args.ink:
        look.ink = args.ink
    if args.cell:
        try:
            width, _, height = args.cell.partition("x")
            look.cell_w = max(24, int(width))
            look.cell_h = max(24, int(height))
        except ValueError:
            print("--cell ждёт ШИРИНАxВЫСОТА, например 32x36", file=sys.stderr)
    return look


def run_test(trigger: str, block_index: int, minutes_worked: int) -> Optional[str]:
    session = store.begin(trigger, block_index, minutes_worked)
    look = ui.Appearance.from_state()
    session.presentation = look.as_state()
    app = TestApp(session, look)
    app.run([sys.argv[0]])

    if app.result is None:
        return None

    # Normally the window already committed and already showed the summary. This
    # is the fallback for the case where it could not -- the record must exist
    # either way.
    if not app.committed:
        store.commit(app.result)

    summary = summary_for(app.result.session_id)
    if summary:
        # Also sent as a notification, which is not a duplicate of the panel: the
        # panel is gone the moment the window is dismissed, and the notification
        # history is where a run can be looked up an hour later.
        #
        # The one case that earns `critical` is the decision to stop working for
        # the day -- the only output that asks for an action rather than
        # reporting a number, and rare by construction (§6 wants two consecutive
        # measurements below the strong cut).
        urgency = "critical" if "прекратить" in summary else "normal"
        notify("Тест внимания", summary, urgency)
    return app.result.session_id


class PromptApp(Gtk.Application):
    """Three buttons and a deadline. Nothing else belongs on this window."""

    def __init__(self, minutes_worked: int, postpone_minutes: int) -> None:
        super().__init__(application_id="dev.mordrud.attention.prompt")
        self.minutes_worked = minutes_worked
        self.postpone_minutes = postpone_minutes
        self.choice = "postpone"
        self._timeout: Optional[int] = None

    def do_activate(self) -> None:
        window = Gtk.ApplicationWindow(application=self, title="Тест внимания")
        window.set_default_size(460, -1)
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
        body.set_text("Две минуты корректурной пробы.")
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
            PROMPT_TIMEOUT_SECONDS, self._timed_out, window
        )
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
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "prompt", "mode", "status", "preview", "look"],
    )
    parser.add_argument("value", nargs="?", default="")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--block", type=int, default=0)
    parser.add_argument("--worked", type=int, default=0)
    parser.add_argument("--postpone-minutes", type=int, default=15)
    parser.add_argument("--font", default="")
    parser.add_argument("--size", type=int, default=0)
    parser.add_argument("--cell", default="", metavar="ШxВ")
    parser.add_argument("--ink", default="", choices=("",) + ui.INK_CHOICES)
    args = parser.parse_args()

    if args.command == "preview":
        # A fixed seed, so switching fonts compares the same letters in the same
        # places instead of a fresh table each time. Nothing is recorded.
        look = appearance_from(args)
        app = TestApp(None, look, preview=True)
        app.run([sys.argv[0]])
        print(look.describe())
        return 0

    if args.command == "look":
        state = store.load_state()
        look = appearance_from(args, state)
        state.update(look.as_state())
        store.save_state(state)
        print(look.describe())
        return 0

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
        return 0

    if args.command == "prompt":
        # Prints the choice and stops there rather than going on to run the test
        # itself. A Gtk.Application registers once per process, so showing a
        # second window after the first has finished is not reliable -- and
        # keeping the branch in timer.sh puts the bookkeeping (thresholds,
        # postpone ladder) in the one place that already owns it.
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
