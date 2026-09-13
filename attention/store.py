# -*- coding: utf-8 -*-
"""Persistence for the attention test.

Two things live here: the session record (one JSON file per run) and the small
piece of state the tool has to remember between runs.

The record is a raw event stream, on purpose. Aggregates can be rebuilt from a
stream; a stream cannot be rebuilt from aggregates. A run is about a hundred
events, so keeping all of them costs nothing and makes any statistic -- including
ones nobody has thought of yet -- computable retroactively over every session
ever recorded. Nothing derived is ever written here.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import grid

# A run is an anchor when this much wall-clock time has passed since the
# previous one. It replaces the calendar day entirely. A calendar boundary was
# tried first and rejected: work often runs past midnight, and a fixed cutoff
# (07:00 was the candidate) is an arbitrary constant that has to be right for
# every schedule. A gap is self-defining -- a night's sleep produces one, a
# 90-minute work interval does not.
ANCHOR_GAP_HOURS = 6.0

STATE_DEFAULTS: Dict[str, str] = {
    # "training" writes full data and shows no verdict; "operational" adds the
    # paired comparison against the day's anchor. §5 of the spec: there is no
    # separate calibration phase, the norm accumulates passively.
    "mode": "training",
    "session_counter": "0",
    "last_session_id": "",
    "last_session_at": "0",
    "last_anchor_id": "",
    # Thresholds and the postpone ladder are NOT here. timer.sh owns them, in
    # study_timer_state, because it is the thing that ticks; a copy in this file
    # would be a second source of truth for the same fact and the two would
    # disagree the first time a run was interrupted.
    # Appearance. The only configurable thing in the tree, because whether a
    # field of 3200 uppercase letters is tiring is not a question the data can
    # answer -- only the one person looking at it can. `preview` turns these.
    "font_family": "JetBrainsMono Nerd Font",
    "font_size": "20",
    "cell_w": "30",
    "cell_h": "34",
    "ink": "foreground",
}


def root_dir() -> str:
    """The study tree, two levels up from this file: study/attention/store.py."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    path = os.path.join(root_dir(), "data", "attention")
    os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
    return path


def sessions_dir() -> str:
    return os.path.join(data_dir(), "sessions")


def state_path() -> str:
    return os.path.join(data_dir(), "state")


# ------------------------------------------------------------------- state
# key=value, matching study_timer_state. Same shape means the same habits work
# on both files -- `cat` is enough to read one, and adding a key never needs a
# parser change.


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
    # that would silently reset the mode back to training.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for key in sorted(state):
            handle.write(f"{key}={state[key]}\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ record


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{random.randrange(16**4):04x}"


def new_seed() -> int:
    # Not from the PRNG in grid -- that one is seeded BY this. os.urandom keeps
    # the seeds themselves unpredictable, so a table cannot be anticipated from
    # the previous one.
    return int.from_bytes(os.urandom(6), "big")


@dataclass
class Context:
    trigger: str = "manual"          # block_end | manual | session_start
    mode: str = "training"
    block_index: int = 0
    minutes_worked_before: int = 0
    is_anchor: bool = False
    anchor_session_id: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "block_index": self.block_index,
            "minutes_worked_before": self.minutes_worked_before,
            "trigger": self.trigger,
            "mode": self.mode,
            "is_anchor": self.is_anchor,
            # Replaces the spec's implicit "first run of the day". A direct
            # reference means the analysis module never has to reconstruct which
            # runs belong together from timestamps.
            "anchor_session_id": self.anchor_session_id,
        }


@dataclass
class Session:
    session_id: str
    session_index: int
    seed: int
    table: grid.Table
    context: Context
    started_at: str = ""
    ended_at: str = ""
    events: List[Dict[str, object]] = field(default_factory=list)
    stop_click: Optional[Dict[str, int]] = None
    # Font, size, cell pitch and ink. These are conditions of measurement, not
    # decoration: a change of size or contrast makes the runs before and after
    # it measurements of different tasks. Recorded so the analysis module can
    # see a change instead of silently pooling across one.
    presentation: Dict[str, str] = field(default_factory=dict)

    def add_event(self, t_ms: int, row: int, col: int, button: str) -> None:
        self.events.append({"t_ms": t_ms, "row": row, "col": col, "button": button})

    def as_dict(self) -> Dict[str, object]:
        return {
            "session_id": self.session_id,
            "session_index": self.session_index,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "context": self.context.as_dict(),
            "generation": {
                "generator_version": grid.GENERATOR_VERSION,
                "rows": grid.ROWS,
                "cols": grid.COLS,
                "alphabet": list(grid.ALPHABET),
                "marker": grid.MARKER,
                "p_target_range": list(grid.P_TARGET_RANGE),
                "p_exc_range": list(grid.P_EXC_RANGE),
                "seed": self.seed,
            },
            "presentation": self.presentation,
            "key": self.table.key,
            "events": self.events,
            "stop_click": self.stop_click,
        }

    def path(self) -> str:
        return os.path.join(sessions_dir(), f"{self.session_id}.json")


def begin(trigger: str, block_index: int, minutes_worked_before: int) -> Session:
    """Allocate a session: index, seed, table, and the anchor decision."""
    state = load_state()

    index = int(state["session_counter"] or 0) + 1
    last_at = float(state["last_session_at"] or 0)
    gap_hours = (time.time() - last_at) / 3600.0 if last_at else float("inf")

    is_anchor = gap_hours >= ANCHOR_GAP_HOURS
    session_id = new_session_id()
    anchor_id = session_id if is_anchor else state["last_anchor_id"]

    # An anchor is missing only before the very first run ever. Falling back to
    # "this run is the anchor" keeps every record paired with something rather
    # than producing an orphan the analysis module has to special-case.
    if not anchor_id:
        is_anchor = True
        anchor_id = session_id

    seed = new_seed()
    session = Session(
        session_id=session_id,
        session_index=index,
        seed=seed,
        table=grid.generate(seed),
        context=Context(
            trigger=trigger,
            mode=state["mode"],
            block_index=block_index,
            minutes_worked_before=minutes_worked_before,
            is_anchor=is_anchor,
            anchor_session_id=anchor_id,
        ),
        started_at=_now_iso(),
    )
    return session


def commit(session: Session) -> str:
    """Write the record, then advance the state. Order matters.

    The record is written first: if the process dies between the two, the run
    survives as data and only the bookkeeping is stale. The reverse order would
    advance the counter past a run that was never saved.
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
    directory = sessions_dir()
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            try:
                records.append(json.load(handle))
            except json.JSONDecodeError:
                # A truncated file is skipped rather than fatal: one bad record
                # must not make every other session unreadable.
                continue
    records.sort(key=lambda r: r.get("session_index", 0))
    return records
