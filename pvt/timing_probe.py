#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Measures the machine, not the person.

A PVT is only worth building if reaction time can be measured to a few
milliseconds. Two things stand between the code and the truth:

  1. The stimulus appears when a FRAME IS PRESENTED, not when queue_draw() is
     called. The gap between the two is the compositor's business and is not
     constant.
  2. The response happens when the key is pressed, not when Python's handler
     gets scheduled. The gap between those is the toolkit's business.

A constant offset in either is harmless -- it is the same in the anchor and in
the measurement and cancels in their ratio. Jitter is not harmless: it goes
straight into the noise floor, and the effects being looked for are 30-100 ms.

So this probe answers three questions with numbers instead of assumptions:
  A. Does the compositor report real presentation times at all?
  B. How much does presentation lag vary?
  C. Do key events carry a usable timestamp, and does it share a clock with the
     frame timings?

Nothing here records anything or judges anyone. Run it, read the numbers.
"""

from __future__ import annotations

import random
import statistics as st
import sys
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

PASSIVE_FRAMES = 180        # about 3 s at 60 Hz
TRIALS = 10
MIN_DELAY_MS = 1000
MAX_DELAY_MS = 4000

# GdkFrameClock keeps only a short history of timings, and presentation feedback
# arrives a frame or two late. This is how many frames we are willing to wait
# for a given frame's timings to become complete before giving up on it.
TIMINGS_PATIENCE = 8


def us_to_ms(value: int) -> float:
    return value / 1000.0


class Probe(Gtk.ApplicationWindow):
    def __init__(self, app) -> None:
        super().__init__(application=app, title="Проверка точности хронометража")
        self.set_default_size(720, 360)

        self.phase = "passive"
        self.stimulus_visible = False

        # Phase A collection
        self.frames_seen = 0
        self.refresh_intervals: List[float] = []
        self.presentation_lags: List[float] = []
        self.presentation_reported = 0
        self.presentation_missing = 0

        # Phase B collection
        self.trial = 0
        self.stim_counter: Optional[int] = None
        self.stim_presented_us: Optional[int] = None
        self.stim_requested_us: Optional[int] = None
        self.waiting_for_key = False
        self.rt_by_event: List[float] = []
        self.rt_by_handler: List[float] = []
        self.dispatch_gap: List[float] = []
        self.event_clock_offset: List[float] = []
        self.pending: List[int] = []

        self.label = Gtk.Label()
        self.label.set_wrap(True)
        self.label.set_margin_start(24)
        self.label.set_margin_end(24)
        self.label.add_css_class("monospace")

        self.area = Gtk.DrawingArea(vexpand=True)
        self.area.set_draw_func(self._draw)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.append(self.label)
        box.append(self.area)
        self.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        self._say("Замеряю кадры…")
        self.connect("realize", self._start)

    # ------------------------------------------------------------- plumbing

    def _say(self, text: str) -> None:
        self.label.set_text(text)

    def _clock(self) -> Gdk.FrameClock:
        return self.get_frame_clock()

    def _start(self, *_args) -> None:
        clock = self._clock()
        # begin_updating keeps frames coming even when nothing changed, which is
        # what makes a passive measurement possible at all.
        clock.begin_updating()
        clock.connect("after-paint", self._after_paint)
        self.area.queue_draw()

    # --------------------------------------------------------------- phase A

    def _after_paint(self, clock: Gdk.FrameClock) -> None:
        counter = clock.get_frame_counter()

        if self.phase == "passive":
            self._collect_passive(clock, counter)
            self.frames_seen += 1
            if self.frames_seen >= PASSIVE_FRAMES:
                self._begin_trials(clock)
            else:
                self.area.queue_draw()
            return

        self._resolve_pending(clock)

    def _collect_passive(self, clock: Gdk.FrameClock, counter: int) -> None:
        # Read a frame a little in the past: presentation feedback for the frame
        # just painted has not arrived yet.
        target = counter - 2
        timings = clock.get_timings(target)
        if timings is None:
            return

        interval = timings.get_refresh_interval()
        if interval:
            self.refresh_intervals.append(us_to_ms(interval))

        presented = timings.get_presentation_time()
        if presented:
            self.presentation_reported += 1
            # How far the real presentation fell from what the frame clock
            # predicted when it started the frame. This is the number that
            # matters: its spread is the jitter the RT inherits.
            predicted = timings.get_predicted_presentation_time()
            if predicted:
                self.presentation_lags.append(us_to_ms(presented - predicted))
        else:
            self.presentation_missing += 1

    # --------------------------------------------------------------- phase B

    def _begin_trials(self, clock: Gdk.FrameClock) -> None:
        self.phase = "trials"
        self._report_passive()
        self._schedule_stimulus()

    def _schedule_stimulus(self) -> None:
        if self.trial >= TRIALS:
            self._finish()
            return
        self.stimulus_visible = False
        self.stim_counter = None
        self.stim_presented_us = None
        self.waiting_for_key = False
        self.area.queue_draw()
        delay = random.randint(MIN_DELAY_MS, MAX_DELAY_MS)
        GLib.timeout_add(delay, self._show_stimulus)

    def _show_stimulus(self) -> bool:
        self.stim_requested_us = GLib.get_monotonic_time()
        self.stimulus_visible = True
        self.waiting_for_key = True
        self.area.queue_draw()
        return False

    def _draw(self, area, cr, width, height) -> None:
        cr.set_source_rgb(0.02, 0.07, 0.13)
        cr.paint()
        if not self.stimulus_visible:
            return
        cr.set_source_rgb(0.98, 0.86, 0.67)
        size = min(width, height) * 0.4
        cr.rectangle((width - size) / 2, (height - size) / 2, size, size)
        cr.fill()
        # The frame counter is captured HERE, inside the draw, so it names the
        # exact frame that first carries the stimulus. Anything captured outside
        # could name a frame drawn before or after it.
        if self.stim_counter is None:
            self.stim_counter = self._clock().get_frame_counter()
            self.pending.append(self.stim_counter)

    def _resolve_pending(self, clock: Gdk.FrameClock) -> None:
        if self.stim_counter is None or self.stim_presented_us is not None:
            return
        if clock.get_frame_counter() - self.stim_counter > TIMINGS_PATIENCE:
            # Feedback never arrived. Fall back to the frame's nominal time --
            # worse, but still tied to the frame rather than to Python.
            timings = clock.get_timings(self.stim_counter)
            if timings is not None:
                self.stim_presented_us = (timings.get_predicted_presentation_time()
                                          or timings.get_frame_time())
            return
        timings = clock.get_timings(self.stim_counter)
        if timings is not None and timings.get_complete():
            self.stim_presented_us = (timings.get_presentation_time()
                                      or timings.get_predicted_presentation_time()
                                      or timings.get_frame_time())

    def _key(self, controller, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval != Gdk.KEY_space or self.phase != "trials":
            return False
        if not self.waiting_for_key:
            return True

        handler_us = GLib.get_monotonic_time()
        event = controller.get_current_event()
        event_ms = event.get_time() if event is not None else 0

        self.waiting_for_key = False
        self.stimulus_visible = False
        self.area.queue_draw()

        # Resolve presentation now if it has not been already.
        self._resolve_pending(self._clock())
        onset_us = self.stim_presented_us or self.stim_requested_us or handler_us

        rt_handler = us_to_ms(handler_us - onset_us)
        self.rt_by_handler.append(rt_handler)

        if event_ms:
            # Event stamps are milliseconds from the compositor; frame timings
            # are microseconds from the monotonic clock. Whether the two share a
            # base is exactly what has to be established, so the offset between
            # them is recorded rather than assumed.
            self.event_clock_offset.append(us_to_ms(handler_us) - event_ms)
            rt_event = event_ms - us_to_ms(onset_us)
            self.rt_by_event.append(rt_event)
            self.dispatch_gap.append(rt_handler - rt_event)

        self.trial += 1
        self._say(f"Проба {self.trial} из {TRIALS}: {rt_handler:.0f} мс")
        GLib.timeout_add(700, lambda: (self._schedule_stimulus(), False)[1])
        return True

    # ---------------------------------------------------------------- output

    def _report_passive(self) -> None:
        print("=" * 66)
        print("A. Кадры")
        total = self.presentation_reported + self.presentation_missing
        if self.refresh_intervals:
            interval = st.median(self.refresh_intervals)
            print(f"   интервал обновления      {interval:.2f} мс "
                  f"({1000/interval:.1f} Гц)")
        else:
            print("   интервал обновления      композитор не сообщает")

        print(f"   кадров опрошено          {total}")
        print(f"   реальное время показа    {self.presentation_reported} "
              f"из {total}")
        if self.presentation_lags:
            v = self.presentation_lags
            print(f"   показ минус предсказание: медиана {st.median(v):+.2f} мс, "
                  f"sd {st.pstdev(v):.2f} мс, размах {min(v):+.2f}…{max(v):+.2f}")
        elif self.presentation_reported:
            print("   предсказанное время показа недоступно — сравнить не с чем")
        print()
        print("B. Десять проб. Пробел по жёлтому квадрату. Esc — выход.")

    def _finish(self) -> None:
        print()
        print("=" * 66)
        print("C. Время реакции")
        if self.rt_by_handler:
            v = self.rt_by_handler
            print(f"   по часам обработчика     медиана {st.median(v):6.1f} мс   "
                  f"sd {st.pstdev(v):5.1f}")
        if self.rt_by_event:
            v = self.rt_by_event
            print(f"   по метке события         медиана {st.median(v):6.1f} мс   "
                  f"sd {st.pstdev(v):5.1f}")
            g = self.dispatch_gap
            print(f"   задержка диспетчеризации медиана {st.median(g):6.1f} мс   "
                  f"sd {st.pstdev(g):5.1f}   размах {min(g):.1f}…{max(g):.1f}")
            o = self.event_clock_offset
            spread = max(o) - min(o)
            print(f"   часы событий и кадров: расхождение {st.median(o):.1f} мс, "
                  f"разброс расхождения {spread:.1f} мс")
            print("   -> общая база часов" if spread < 5
                  else "   -> базы часов РАЗНЫЕ, метки событий использовать нельзя")
        else:
            print("   метки времени событий недоступны — придётся мерить в обработчике")
        print("=" * 66)
        self.close()


class App(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.mordrud.pvt.probe")

    def do_activate(self) -> None:
        Probe(self).present()


if __name__ == "__main__":
    sys.exit(App().run([sys.argv[0]]))
