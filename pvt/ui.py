# -*- coding: utf-8 -*-
"""The PVT surface: a fixation box, a counter that starts running, a spacebar.

The whole instrument is the timing, so the two moments that matter are captured
the way the probe on this machine validated them:

  onset    the frame counter is taken INSIDE the draw function, so it names the
           exact frame that first carries the counter; its real presentation
           time is then read back from GdkFrameTimings once the compositor's
           feedback arrives (178 of 178 frames reported it here).
  response the GDK event's own timestamp, not the clock read inside the handler.
           The probe measured 0.8 ms median between the two, and confirmed both
           sit on the same clock base, so the event stamp is used directly.

Everything else on screen is subordinate to not disturbing those two numbers.
"""

from __future__ import annotations

import os
import statistics as st
from typing import Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import store  # noqa: E402

WINDOW_W = 900
WINDOW_H = 520

# Prose never reaches closer than this to the window edge.
TEXT_MARGIN = 40

# Between two paragraphs. Has to exceed the line step inside a paragraph, or the
# blocks read as one run of text -- which is what happened at 14 px against a
# 26 px line step.
PARAGRAPH_GAP = 34

BOX_W = 420
BOX_H = 170
FONT_FAMILY = "JetBrainsMono Nerd Font"
COUNTER_SIZE = 76

# How many frames to wait for presentation feedback before falling back to the
# frame's predicted time. On this machine feedback has always arrived, but a
# missing report must degrade rather than hang.
TIMINGS_PATIENCE = 8

PHASE_INTRO = "intro"
PHASE_WAIT = "wait"        # inter-stimulus interval
PHASE_STIMULUS = "stimulus"
PHASE_FEEDBACK = "feedback"
PHASE_RATING = "rating"
PHASE_DONE = "done"

# Karolinska Sleepiness Scale, all nine anchors labelled. The standard companion
# to the PVT in the sleep literature, taken as published rather than invented:
# using the same instrument is what lets any published relationship between
# rating and lapse rate carry over.
KSS = (
    (1, "крайне бодр"),
    (2, "очень бодр"),
    (3, "бодр"),
    (4, "скорее бодр"),
    (5, "ни бодр, ни сонлив"),
    (6, "есть признаки сонливости"),
    (7, "сонлив, но бодрствовать легко"),
    (8, "сонлив, бодрствовать стоит усилий"),
    (9, "очень сонлив, борюсь со сном"),
)

THEME_COLORS = os.path.expanduser(
    "~/.local/state/omarchy/current/theme/colors.toml"
)
FALLBACK = {
    "dark_background": "#031222",
    "lighter_background": "#0a2540",
    "foreground": "#f6dcac",
    "light_foreground": "#a7c9c6",
    "muted": "#2a6b78",
    "accent": "#faa968",
    "red": "#f85525",
}


def _hex_to_rgb(value: str) -> Tuple[float, float, float]:
    value = value.strip().strip('"').lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore


def load_palette() -> Dict[str, Tuple[float, float, float]]:
    raw = dict(FALLBACK)
    try:
        with open(THEME_COLORS, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key, value = key.strip(), value.strip().strip('"')
                if key in FALLBACK and value.startswith("#") and len(value) == 7:
                    raw[key] = value
    except OSError:
        pass
    return {k: _hex_to_rgb(v) for k, v in raw.items()}


class PvtWindow(Gtk.ApplicationWindow):
    def __init__(self, app, session: store.Session, on_finish) -> None:
        super().__init__(application=app, title="Тест бдительности")
        self.session = session
        self.on_finish = on_finish
        self.palette = load_palette()

        self.set_default_size(WINDOW_W, WINDOW_H)
        self.set_resizable(False)

        self.phase = PHASE_INTRO
        self.run_start_us: Optional[int] = None
        # Recorded trials, and a separate cursor over the interval sequence. A
        # false start consumes an interval without producing a trial, so the two
        # must not be the same counter -- otherwise the redrawn wait would reuse
        # the interval the subject just anticipated, which is the one thing the
        # redraw exists to prevent.
        self.trial_index = 0
        self.isi_cursor = 0
        self.current_isi = 0

        # Current trial
        self.onset_frame: Optional[int] = None
        self.onset_us: Optional[int] = None
        self.onset_requested_us: Optional[int] = None
        self.shown_rt_ms: Optional[float] = None
        self.pending_false_starts: List[float] = []
        self.timeout_id: Optional[int] = None

        # Spacebar autorepeat would otherwise manufacture false starts by the
        # dozen. A press only counts once the key has been released.
        self.key_down = False

        # Which device answered this trial, and whether the window kept focus
        # through it. Hyprland swallows the click that refocuses a window, so a
        # trial spanning a focus change is not a measurement.
        self.response_kind = "key"
        self.focus_ok = True

        # Frame-timing quality, accumulated over the run and stored with it.
        self.lag_samples: List[float] = []
        self.frames_total = 0
        self.frames_with_presentation = 0

        self.area = Gtk.DrawingArea(vexpand=True)
        self.area.set_draw_func(self._draw)
        self.set_child(self.area)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        keys.connect("key-released", self._key_released)
        self.add_controller(keys)

        # The left mouse button answers as well as the spacebar. Both are
        # recorded per trial and never pooled: measured on this machine the
        # mouse is 65 ms FASTER than the spacebar, the opposite of the usual
        # expectation and far larger than any fatigue effect.
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._clicked)
        self.area.add_controller(click)

        self.connect("notify::is-active", self._focus_changed)
        self.connect("realize", self._realized)

    # ------------------------------------------------------------- plumbing

    def _clock(self) -> Gdk.FrameClock:
        return self.get_frame_clock()

    def _realized(self, *_args) -> None:
        clock = self._clock()
        # Frames are kept coming for the whole run. The counter needs them to
        # animate, and presentation feedback needs them to arrive at all; the
        # draw is a few shapes, so the cost is irrelevant next to the benefit of
        # never having to restart the clock at the moment that matters most.
        clock.begin_updating()
        clock.connect("after-paint", self._after_paint)

    def _focus_changed(self, *_args) -> None:
        if not self.get_property("is-active"):
            self.focus_ok = False

    def _now_ms(self) -> float:
        if self.run_start_us is None:
            return 0.0
        return (GLib.get_monotonic_time() - self.run_start_us) / 1000.0

    def _cancel_timeout(self) -> None:
        if self.timeout_id is not None:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = None

    # ---------------------------------------------------------------- frames

    def _after_paint(self, clock: Gdk.FrameClock) -> None:
        self._collect_timing(clock)
        self._resolve_onset(clock)
        # begin_updating() keeps the frame clock ticking, but a DrawingArea
        # still only repaints when asked. Asking every frame is what makes the
        # counter run -- and, more importantly, guarantees that the frame which
        # first carries the stimulus is drawn on the very next tick after the
        # phase changes, with nothing scheduled in between.
        if self.phase in (PHASE_INTRO, PHASE_WAIT, PHASE_STIMULUS,
                          PHASE_FEEDBACK):
            self.area.queue_draw()

    def _collect_timing(self, clock: Gdk.FrameClock) -> None:
        timings = clock.get_timings(clock.get_frame_counter() - 2)
        if timings is None:
            return
        self.frames_total += 1
        presented = timings.get_presentation_time()
        if not presented:
            return
        self.frames_with_presentation += 1
        predicted = timings.get_predicted_presentation_time()
        if predicted:
            self.lag_samples.append((presented - predicted) / 1000.0)

    def _resolve_onset(self, clock: Gdk.FrameClock) -> None:
        if self.onset_frame is None or self.onset_us is not None:
            return
        timings = clock.get_timings(self.onset_frame)
        overdue = clock.get_frame_counter() - self.onset_frame > TIMINGS_PATIENCE
        if timings is None:
            if overdue:
                self.onset_us = self.onset_requested_us
            return
        if timings.get_complete() or overdue:
            self.onset_us = (timings.get_presentation_time()
                             or timings.get_predicted_presentation_time()
                             or timings.get_frame_time()
                             or self.onset_requested_us)

    # ----------------------------------------------------------- trial cycle

    def _start_run(self) -> None:
        self.run_start_us = GLib.get_monotonic_time()
        self._next_trial()

    def _next_trial(self, after_feedback: bool = False) -> None:
        # The run ends on the clock, not on a trial count. A trial already in
        # flight is allowed to finish; a new one is not started past the line.
        if self._now_ms() >= store.DURATION_S * 1000:
            self._finish()
            return
        if self.isi_cursor >= len(self.session.isis):
            self._finish()
            return

        self.phase = PHASE_WAIT
        self.onset_frame = None
        self.onset_us = None
        self.onset_requested_us = None
        self.shown_rt_ms = None
        self.focus_ok = self.get_property("is-active")
        self.current_isi = self.session.isis[self.isi_cursor]
        self.isi_cursor += 1

        # The published interval INCLUDES the second of feedback -- it is the
        # gap from one response to the next stimulus, not extra time added after
        # the feedback has finished. Adding it instead of absorbing it stretched
        # the effective interval to 2-5 s and cost about a third of the trials
        # in the first real run: 68 where the protocol yields near 100.
        delay = self.current_isi
        if after_feedback:
            delay = max(0, delay - store.FEEDBACK_MS)
        self.timeout_id = GLib.timeout_add(delay, self._show_stimulus)

    def _show_stimulus(self) -> bool:
        self.timeout_id = None
        self.onset_requested_us = GLib.get_monotonic_time()
        self.phase = PHASE_STIMULUS
        self.timeout_id = GLib.timeout_add(store.MAX_RT_MS, self._abandon_trial)
        return False

    def _abandon_trial(self) -> bool:
        """No response inside the cap. Recorded as a trial with no reaction."""
        self.timeout_id = None
        self._record(None)
        self.phase = PHASE_FEEDBACK
        self.shown_rt_ms = None
        self.timeout_id = GLib.timeout_add(store.FEEDBACK_MS, self._after_feedback)
        return False

    def _after_feedback(self) -> bool:
        self.timeout_id = None
        self.trial_index += 1
        self._next_trial(after_feedback=True)
        return False

    def _record(self, rt_ms: Optional[float]) -> None:
        onset_ms = 0.0
        if self.onset_us is not None and self.run_start_us is not None:
            onset_ms = (self.onset_us - self.run_start_us) / 1000.0
        elif self.onset_requested_us is not None and self.run_start_us is not None:
            onset_ms = (self.onset_requested_us - self.run_start_us) / 1000.0

        # False starts were collected as run-relative times; they belong to the
        # trial as offsets from its onset, which is only known now.
        offsets = [t - onset_ms for t in self.pending_false_starts]
        self.pending_false_starts = []

        self.session.trials.append(store.Trial(
            index=self.trial_index,
            isi_ms=self.current_isi,
            onset_ms=onset_ms,
            rt_ms=rt_ms,
            false_starts=offsets,
            response=self.response_kind,
            focus_ok=self.focus_ok,
        ))

    def _false_start(self) -> None:
        """A press before the stimulus. The interval is redrawn, as in the
        standard protocol: without that, mashing the key would eventually catch
        a stimulus and be rewarded with an impossibly good reaction time."""
        self.pending_false_starts.append(self._now_ms())
        self._cancel_timeout()
        self.phase = PHASE_FEEDBACK
        self.shown_rt_ms = -1.0  # marker: draw the false-start message
        self.timeout_id = GLib.timeout_add(store.FEEDBACK_MS, self._restart_wait)

    def _restart_wait(self) -> bool:
        self.timeout_id = None
        self.shown_rt_ms = None
        self._next_trial(after_feedback=True)
        return False

    # --------------------------------------------------------------- input

    def _key_released(self, controller, keyval, keycode, state) -> None:
        if keyval == Gdk.KEY_space:
            self.key_down = False

    def _key_pressed(self, controller, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            # A skipped rating is recorded as absent rather than as a value. The
            # run itself is already spent and must not be lost over it.
            if self.phase == PHASE_RATING:
                self._rated(None)
                return True
            # After the run is over the record already exists and the summary is
            # on screen; Esc there just closes the window.
            if self.phase == PHASE_DONE:
                self.close()
                return True
            self._cancel_timeout()
            self.on_finish(None)
            self.close()
            return True

        if self.phase == PHASE_RATING:
            if Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
                self._rated(keyval - Gdk.KEY_1 + 1)
                return True
            return True

        if keyval != Gdk.KEY_space:
            return False
        if self.key_down:
            return True          # autorepeat
        self.key_down = True
        self._respond("key", controller.get_current_event())
        return True

    def _clicked(self, gesture, n_press, x, y) -> None:
        self._respond("mouse", gesture.get_current_event())

    def _respond(self, kind: str, event) -> None:
        """One path for both devices, so nothing can drift between them."""
        if self.phase == PHASE_INTRO:
            self.response_kind = kind
            self.phase = PHASE_WAIT
            self._start_run()
            return

        if self.phase == PHASE_WAIT:
            self.response_kind = kind
            self._false_start()
            return

        if self.phase != PHASE_STIMULUS:
            return

        event_ms = event.get_time() if event is not None else 0
        self._resolve_onset(self._clock())
        onset_us = self.onset_us or self.onset_requested_us or GLib.get_monotonic_time()

        if event_ms:
            # Same clock base, confirmed by the probe: 0.8 ms median offset with
            # 0.8 ms spread. Using the event stamp skips the handler-dispatch
            # delay entirely, for the mouse exactly as for the key.
            rt = event_ms - onset_us / 1000.0
        else:
            rt = (GLib.get_monotonic_time() - onset_us) / 1000.0

        # A negative or absurd value means the onset was resolved wrongly, not
        # that the reaction was impossible. Clamping would hide that; it is
        # recorded and left for the analysis to reject.
        self.response_kind = kind
        self._cancel_timeout()
        self._record(rt)
        self.phase = PHASE_FEEDBACK
        self.shown_rt_ms = rt
        self.timeout_id = GLib.timeout_add(store.FEEDBACK_MS, self._after_feedback)

    # -------------------------------------------------------------- drawing

    def _draw(self, area, cr, width, height) -> None:
        p = self.palette
        cr.set_source_rgb(*p["dark_background"])
        cr.paint()

        box_x = (width - BOX_W) / 2
        box_y = (height - BOX_H) / 2 - 20

        # The fixation box is always on screen. The stimulus is the counter
        # starting to run inside it, exactly as on the PVT-192 -- not a shape
        # appearing somewhere, which would add a visual search the task is not
        # supposed to contain.
        cr.set_source_rgb(*p["muted"])
        cr.set_line_width(2.0)
        cr.rectangle(box_x, box_y, BOX_W, BOX_H)
        cr.stroke()

        cr.select_font_face(FONT_FAMILY)

        if self.phase == PHASE_INTRO:
            y = self._block(cr, width, box_y + BOX_H + 60,
                            "Пробел или левая кнопка мыши — начать и отвечать, "
                            "как только в рамке побегут цифры.",
                            18, p["light_foreground"])
            self._block(cr, width, y + PARAGRAPH_GAP,
                        f"{store.DURATION_S // 60} минут. Отвечайте всё время "
                        f"одним и тем же: устройства различаются на десятки "
                        f"миллисекунд, смешивать нельзя. Esc — прервать без записи.",
                        16, p["muted"])
            return

        # No time remaining anywhere on screen. A visible clock invites an
        # end spurt, and an end spurt in a vigilance task is a documented
        # artefact -- the last minute would stop being comparable with the
        # first, which is precisely the comparison the whole scheme rests on.
        text, colour = None, p["foreground"]
        if self.phase == PHASE_STIMULUS:
            # Captured here and nowhere else: this is the only place that knows
            # which frame is the first one carrying the counter. Taken outside
            # the draw it would name the frame before or after it, and a single
            # frame is 7 ms on this display -- a third of the effect being
            # looked for.
            if self.onset_frame is None:
                self.onset_frame = self._clock().get_frame_counter()
            text = f"{self._now_ms() - self._onset_ms():.0f}"
        elif self.phase == PHASE_FEEDBACK:
            if self.shown_rt_ms is None:
                text, colour = "—", p["muted"]
            elif self.shown_rt_ms < 0:
                text, colour = "рано", p["red"]
            else:
                text = f"{self.shown_rt_ms:.0f}"

        if text is not None:
            cr.set_font_size(COUNTER_SIZE)
            cr.set_source_rgb(*colour)
            ext = cr.text_extents(text)
            cr.move_to(box_x + (BOX_W - ext.width) / 2 - ext.x_bearing,
                       box_y + (BOX_H - ext.height) / 2 - ext.y_bearing)
            cr.show_text(text)

    def _onset_ms(self) -> float:
        if self.onset_us is not None and self.run_start_us is not None:
            return (self.onset_us - self.run_start_us) / 1000.0
        if self.onset_requested_us is not None and self.run_start_us is not None:
            return (self.onset_requested_us - self.run_start_us) / 1000.0
        return self._now_ms()

    def _centred(self, cr, width: float, y: float, text: str,
                 size: float, colour) -> None:
        cr.set_font_size(size)
        cr.set_source_rgb(*colour)
        ext = cr.text_extents(text)
        cr.move_to((width - ext.width) / 2 - ext.x_bearing, y)
        cr.show_text(text)

    def _block(self, cr, width: float, y: float, text: str,
               size: float, colour) -> float:
        """Centred text wrapped to the window. Returns the y after the last line.

        Cairo's toy text API does not wrap, so a sentence longer than the window
        simply runs off both edges -- which is what happened the first time this
        screen was shown. Wrapping rather than widening the window: the text can
        change again, the window cannot follow it every time, and the window's
        width is tied to nothing here so there is no reason to let prose set it.
        """
        cr.set_font_size(size)
        cr.set_source_rgb(*colour)
        limit = width - 2 * TEXT_MARGIN

        lines, current = [], ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if current and cr.text_extents(candidate).width > limit:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)

        step = size * 1.45
        for index, line in enumerate(lines):
            ext = cr.text_extents(line)
            cr.move_to((width - ext.width) / 2 - ext.x_bearing, y + index * step)
            cr.show_text(line)
        return y + (len(lines) - 1) * step

    # ---------------------------------------------------------------- finish

    def _finish(self) -> None:
        self._cancel_timeout()
        self.phase = PHASE_RATING
        self.session.timing = {
            "frames": self.frames_total,
            "presentation_reported": round(
                self.frames_with_presentation / self.frames_total, 3)
            if self.frames_total else 0.0,
            "lag_median_ms": round(st.median(self.lag_samples), 2)
            if self.lag_samples else 0.0,
            "lag_sd_ms": round(st.pstdev(self.lag_samples), 2)
            if len(self.lag_samples) > 1 else 0.0,
        }
        self._ask_rating()

    # ---------------------------------------------------------------- rating

    def _ask_rating(self) -> None:
        """Asked after the run and before any numbers are shown.

        Order matters more than it looks. A rating given after the summary is
        partly a reading of the summary, and the correlation between the two
        would then be partly a correlation of the measurement with itself. Asked
        before the test instead, it would prime the run it is meant to describe.
        Between the two is the only clean place.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(60)
        box.set_margin_end(60)

        title = Gtk.Label(xalign=0.5)
        title.set_markup("<b>Как вы себя чувствовали последние минуты?</b>")
        box.append(title)

        hint = Gtk.Label(xalign=0.5)
        hint.set_markup("<small>Клавиши 1–9. Esc — пропустить.</small>")
        box.append(hint)

        for value, label in KSS:
            button = Gtk.Button(label=f"{value}  —  {label}")
            button.connect("clicked", lambda _b, v=value: self._rated(v))
            box.append(button)

        self.set_child(box)
        self.set_title("Тест бдительности — самооценка")

    def _rated(self, value: Optional[int]) -> None:
        if self.phase != PHASE_RATING:
            return
        self.phase = PHASE_DONE
        if value is not None:
            self.session.subjective = {
                "scale": "KSS",
                "value": value,
                "label": dict(KSS)[value],
                "asked": "after_run_before_summary",
            }
        self._show_summary()

    def _show_summary(self) -> None:
        summary = self.on_finish(self.session)
        if not summary:
            self.close()
            return
        panel = Gtk.Label(xalign=0.5, yalign=0.5, vexpand=True)
        panel.add_css_class("monospace")
        panel.set_text(summary)
        self.set_child(panel)
        self.set_title("Тест бдительности — записано")
