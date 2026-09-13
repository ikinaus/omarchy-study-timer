# -*- coding: utf-8 -*-
"""Persistence and trial sequencing for the PVT.

Same principle as the cancellation test it replaces on the timer: the record is
a raw stream and nothing derived is written into it. Here that principle has a
sharper edge than usual. The standard lapse threshold is 355 ms, calibrated on
people whose baseline is around 280 ms; this subject's baseline came out near
200 ms in the timing probe, so the threshold will almost certainly have to be
re-derived from his own data. If "lapse" were stored per trial, that re-derivation
would invalidate every session already collected. Storing the reaction time and
classifying at read time costs nothing and keeps the whole archive live.

The generator is seeded and deterministic, so a session's inter-stimulus
sequence can be reproduced from the record without storing it -- though it is
stored anyway, because the realised sequence is small and the seed only protects
against the sequence being lost, not against the code changing.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

PROTOCOL_NAME = "PVT-B"
# Bump when any parameter below changes in a way that makes old and new runs
# incomparable. Every record carries it, so two epochs can always be told apart
# instead of silently pooled.
# v1 -> v2 (2026-09-05): the feedback second was being ADDED after the interval
# instead of occupying its first second, which stretched the effective foreperiod
# to 2-5 s and cut a five-minute run from ~108 trials to 68. Session 1 was
# recorded under v1 and must never be pooled with what follows: reaction time
# depends strongly on foreperiod length -- measured on this subject, median RT
# runs 284 ms after a 1-1.75 s wait and 229 ms after a 3.25-4 s one -- so the
# two versions measure at different points on that curve.
PROTOCOL_VERSION = 2

# Five minutes, not the three of the published PVT-B. The published length was
# chosen to be as short as possible; the 2022 convergent-validity work found the
# 3-minute version weakest exactly where this tool needs it most -- on the
# rested baseline that every later measurement is divided by.
DURATION_S = 300

# PVT-B's own range, kept. Wider intervals (the 10-minute PVT uses 2-10 s)
# suppress anticipation better but would drop this session from ~110 trials to
# ~48, taking the standard error of mean 1/RT from about 1.9% to 2.9%.
# Anticipation is already penalised: a press before the stimulus is recorded as
# a false start.
ISI_MIN_MS = 1000
ISI_MAX_MS = 4000

# The counter stays on screen this long after a response. Part of the ISI budget
# in the published protocol, so it is not extra time.
FEEDBACK_MS = 1000

# A response faster than this cannot be a reaction to the stimulus.
VALID_RT_MIN_MS = 100

# Recorded, not applied. See the module docstring.
LAPSE_MS_NOMINAL = 355

# A real lapse can run to two or three seconds. Beyond this the trial is
# abandoned so one microsleep cannot eat the rest of the session.
MAX_RT_MS = 5000

# A run is an anchor when this much wall-clock time has passed since the
# previous one.
ANCHOR_GAP_HOURS = 6.0

STATE_DEFAULTS: Dict[str, str] = {
    "mode": "training",
    "session_counter": "0",
    "last_session_id": "",
    "last_session_at": "0",
    "last_anchor_id": "",
}


class PCG32:
    """Minimal PCG-XSH-RR 64/32.

    Deliberately a second copy of the one in the cancellation test rather than a
    shared import. The two tools are now separate instruments, and a change made
    for one must not be able to alter the other's recorded sequences.
    """

    _MASK = (1 << 64) - 1
    _MULT = 6364136223846793005

    def __init__(self, seed: int, seq: int = 54) -> None:
        self.state = 0
        self.inc = ((seq << 1) | 1) & self._MASK
        self.next_uint32()
        self.state = (self.state + (seed & self._MASK)) & self._MASK
        self.next_uint32()

    def next_uint32(self) -> int:
        old = self.state
        self.state = (old * self._MULT + self.inc) & self._MASK
        xorshifted = (((old >> 18) ^ old) >> 27) & 0xFFFFFFFF
        rot = (old >> 59) & 31
        return ((xorshifted >> rot) | (xorshifted << ((32 - rot) & 31))) & 0xFFFFFFFF

    def random(self) -> float:
        return self.next_uint32() / 4294967296.0

    def between(self, lo: int, hi: int) -> int:
        return lo + int(self.random() * (hi - lo + 1))


def isi_sequence(seed: int, count: int) -> List[int]:
    """Inter-stimulus intervals in ms, uniform over [ISI_MIN_MS, ISI_MAX_MS]."""
    rng = PCG32(seed)
    return [rng.between(ISI_MIN_MS, ISI_MAX_MS) for _ in range(count)]


def max_trials() -> int:
    """Enough intervals to outlast the session however fast the responses are.

    The run ends on the clock, not on a trial count, so this only has to be an
    upper bound: the shortest possible cycle is the minimum interval plus a
    minimal reaction.
    """
    return int(DURATION_S * 1000 / ISI_MIN_MS) + 2


# ------------------------------------------------------------------- paths


def root_dir() -> str:
    """The study tree, two levels up from this file: study/pvt/store.py."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    path = os.path.join(root_dir(), "data", "pvt")
    os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
    return path


def sessions_dir() -> str:
    return os.path.join(data_dir(), "sessions")


def state_path() -> str:
    return os.path.join(data_dir(), "state")


def timer_state_path() -> str:
    return os.path.join(root_dir(), "data", "study_timer_state")


# ------------------------------------------------------------ timer snapshot


def timer_snapshot() -> Dict[str, object]:
    """What the study timer knew at the moment of the run.

    Read straight from the timer's state file rather than passed in as flags.
    The run can be launched three ways -- by hand, by the prompt helper, by the
    timer itself -- and threading the same five values through each path is how
    they end up disagreeing. One reader, one source.

    `minutes_worked_before` in the context is the day's total, which is what the
    launcher passes. It cannot distinguish 105 minutes in one unbroken stretch
    from 105 minutes in three, and fatigue does not treat those alike; nor does
    it say how long the break before the stretch was, which is the closest thing
    to a sleep measure available here. Both are recorded now.
    """
    empty: Dict[str, object] = {
        "available": False, "running": None, "day": None,
        "worked_today_min": None, "stretch_min": None,
        "idle_before_stretch_min": None, "idle_min": None,
        "away_gap_min": None, "since_return_min": None,
    }
    try:
        raw: Dict[str, str] = {}
        with open(timer_state_path(), encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep:
                    raw[key] = value

        def num(key: str) -> int:
            return int(raw.get(key, "0") or 0)

        now = int(time.time())
        running = num("running") == 1
        since, total = num("since"), num("total")
        worked = total + (now - since) if running else total
        away_gap, back_at = num("away_gap"), num("back_at")

        return {
            "available": True,
            "running": running,
            "day": raw.get("day"),
            "worked_today_min": worked // 60,
            # Zero while stopped: there is no stretch in progress to measure.
            "stretch_min": ((now - since) // 60) if running else 0,
            # Only meaningful while running -- time spent AT the machine before
            # this stretch began, with any absence taken out. Sleep is not in
            # here; that is `away_gap_min` below.
            "idle_before_stretch_min": (num("idle_before_start") // 60
                                        if running else None),
            # And its counterpart while stopped: how much of the current break
            # has been spent sitting here, again excluding absence.
            "idle_min": (None if running else num("idle_present") // 60),
            # Absence from the MACHINE, not from the clock. These two are what
            # decide whether a run deserves to be called an anchor: the first is
            # how long he was gone, the second how long he has been back. The
            # timer's own rule reads the same pair, but recording them lets the
            # rule be re-drawn later over runs already taken -- which is the only
            # way to answer it, since "hasn't tested in a while" is not "rested".
            "away_gap_min": (away_gap // 60) if away_gap else None,
            "since_return_min": (max(0, (now - back_at) // 60)
                                 if back_at else None),
        }
    except (OSError, ValueError):
        # The timer is not required for the test to run. A missing or unreadable
        # state file costs the context, never the measurement.
        return empty


# ------------------------------------------------------------------- state


def load_state() -> Dict[str, str]:
    state = dict(STATE_DEFAULTS)
    path = state_path()
    if not os.path.exists(path):
        return state
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            state[key] = value
    return state


def save_state(state: Dict[str, str]) -> None:
    path = state_path()
    # Written to a sibling and renamed: rename is atomic within a filesystem, so
    # a crash mid-write leaves the old state intact rather than a truncated file
    # that would silently reset the mode.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for key in sorted(state):
            handle.write(f"{key}={state[key]}\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ record


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{random.randrange(16**4):04x}"


def new_seed() -> int:
    return int.from_bytes(os.urandom(6), "big")


@dataclass
class Trial:
    index: int
    isi_ms: int
    # Milliseconds from the start of the run to the moment the stimulus frame was
    # actually presented -- not to the moment it was requested.
    onset_ms: float
    # None when the trial was abandoned at MAX_RT_MS. Values below
    # VALID_RT_MIN_MS are kept as recorded; classifying them is the analysis
    # module's job.
    rt_ms: Optional[float] = None
    # Presses that landed before the stimulus, as offsets from onset (negative).
    # An empty list is the normal case.
    false_starts: List[float] = field(default_factory=list)
    # "key" or "mouse". A mouse click is reliably 20-30 ms slower than a key
    # press -- that is motor cost, not vigilance -- so the two are never pooled.
    response: str = "key"
    # False when the window lost focus at any point during this trial. Hyprland
    # does not deliver the click that restores focus to the client, so a
    # response after a focus change is swallowed and the next one carries the
    # whole detour in its reaction time. Such a trial is an artefact of the
    # compositor, not a measurement.
    focus_ok: bool = True

    def as_dict(self) -> Dict[str, object]:
        return {
            "i": self.index,
            "isi_ms": self.isi_ms,
            "onset_ms": round(self.onset_ms, 1),
            "rt_ms": None if self.rt_ms is None else round(self.rt_ms, 1),
            "false_starts": [round(x, 1) for x in self.false_starts],
            "response": self.response,
            "focus_ok": self.focus_ok,
        }


@dataclass
class Context:
    trigger: str = "manual"          # block_end | manual | session_start
    mode: str = "training"
    block_index: int = 0
    minutes_worked_before: int = 0
    is_anchor: bool = False
    anchor_session_id: str = ""
    # Wall-clock hours since the previous run, and the local hour this one
    # started. Neither is used by the anchor rule as it stands. They are stored
    # because the rule is known to be wrong -- it fires on "has not tested in a
    # while", which is not "is rested", and once produced an anchor from the
    # most degraded run on record. Keeping both makes that fixable in hindsight
    # over sessions already collected.
    hours_since_previous: Optional[float] = None
    local_hour: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "block_index": self.block_index,
            "minutes_worked_before": self.minutes_worked_before,
            "trigger": self.trigger,
            "mode": self.mode,
            "is_anchor": self.is_anchor,
            "anchor_session_id": self.anchor_session_id,
            "hours_since_previous": (None if self.hours_since_previous is None
                                     else round(self.hours_since_previous, 2)),
            "local_hour": self.local_hour,
        }


@dataclass
class Session:
    session_id: str
    session_index: int
    seed: int
    isis: List[int]
    context: Context
    started_at: str = ""
    ended_at: str = ""
    trials: List[Trial] = field(default_factory=list)
    # The Karolinska Sleepiness Scale rating given after the run and before the
    # summary. None when skipped. Stored raw, like everything else here: what
    # counts as "sleepy" is an analysis decision, not a property of the run.
    subjective: Optional[Dict[str, object]] = None
    # What the study timer knew when the run started. Captured at begin(), not
    # at commit(): by the time the run ends five minutes have passed and the
    # clock may have been stopped or started in between.
    timer: Dict[str, object] = field(default_factory=dict)
    # Frame-timing quality measured during this run. The probe showed good
    # numbers on an idle machine; a run taken while something heavy is compiling
    # may not be comparable, and that has to be visible in the data rather than
    # guessed at afterwards.
    timing: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "session_id": self.session_id,
            "session_index": self.session_index,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "context": self.context.as_dict(),
            "protocol": {
                "name": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION,
                "duration_s": DURATION_S,
                "isi_range_ms": [ISI_MIN_MS, ISI_MAX_MS],
                "feedback_ms": FEEDBACK_MS,
                "valid_rt_min_ms": VALID_RT_MIN_MS,
                "lapse_ms_nominal": LAPSE_MS_NOMINAL,
                "max_rt_ms": MAX_RT_MS,
                "response": "space",
                "seed": self.seed,
            },
            "subjective": self.subjective,
            "timer": self.timer,
            "timing": self.timing,
            "trials": [t.as_dict() for t in self.trials],
        }

    def path(self) -> str:
        return os.path.join(sessions_dir(), f"{self.session_id}.json")


def begin(trigger: str, block_index: int, minutes_worked_before: int) -> Session:
    """Allocate a session: index, seed, interval sequence, anchor decision."""
    state = load_state()

    index = int(state["session_counter"] or 0) + 1
    last_at = float(state["last_session_at"] or 0)
    gap_hours = (time.time() - last_at) / 3600.0 if last_at else None

    is_anchor = gap_hours is None or gap_hours >= ANCHOR_GAP_HOURS
    session_id = new_session_id()
    anchor_id = session_id if is_anchor else state["last_anchor_id"]

    if not anchor_id:
        is_anchor = True
        anchor_id = session_id

    seed = new_seed()
    return Session(
        session_id=session_id,
        session_index=index,
        seed=seed,
        isis=isi_sequence(seed, max_trials()),
        context=Context(
            trigger=trigger,
            mode=state["mode"],
            block_index=block_index,
            minutes_worked_before=minutes_worked_before,
            is_anchor=is_anchor,
            anchor_session_id=anchor_id,
            hours_since_previous=gap_hours,
            local_hour=datetime.now().hour,
        ),
        started_at=_now_iso(),
        timer=timer_snapshot(),
    )


def commit(session: Session) -> str:
    """Write the record, then advance the state. Order matters.

    The record goes first: if the process dies between the two, the run survives
    as data and only the bookkeeping is stale. The reverse order would advance
    the counter past a run that was never saved.
    """
    session.ended_at = _now_iso()

    path = session.path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(session.as_dict(), handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    os.replace(tmp, path)

    state = load_state()
    state["session_counter"] = str(session.session_index)
    state["last_session_id"] = session.session_id
    state["last_session_at"] = str(int(time.time()))
    state["last_anchor_id"] = session.context.anchor_session_id
    save_state(state)

    return path


def load_all() -> List[Dict[str, object]]:
    """Every recorded session, oldest first. The analysis module's only input."""
    records = []
    for name in sorted(os.listdir(sessions_dir())):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(sessions_dir(), name), encoding="utf-8") as handle:
            try:
                records.append(json.load(handle))
            except json.JSONDecodeError:
                # A truncated file is skipped rather than fatal: one bad record
                # must not make every other session unreadable.
                continue
    records.sort(key=lambda r: r.get("session_index", 0))
    return records


if __name__ == "__main__":
    import statistics as stats

    seq = isi_sequence(12345, max_trials())
    same = isi_sequence(12345, max_trials())
    other = isi_sequence(12346, max_trials())

    assert seq == same, "same seed must give the same sequence"
    assert seq != other, "different seeds must differ"
    assert all(ISI_MIN_MS <= x <= ISI_MAX_MS for x in seq), "interval out of range"

    mean_isi = stats.mean(seq)
    cycle_ms = mean_isi + 250  # a plausible reaction on top of each interval
    print(f"интервалов заготовлено   {len(seq)}")
    print(f"интервал: среднее {mean_isi:.0f} мс, "
          f"размах {min(seq)}–{max(seq)} мс, номинал {(ISI_MIN_MS+ISI_MAX_MS)/2:.0f}")
    print(f"ожидаемо проб за {DURATION_S} с: примерно {DURATION_S*1000/cycle_ms:.0f}")
    print(f"воспроизводимость: одно зерно — одна последовательность, разные — разные")
    print(f"каталог данных: {data_dir()}")
