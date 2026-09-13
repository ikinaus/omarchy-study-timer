#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Analysis over the accumulated attention-test records.

Deliberately not part of the test. The test writes an event stream and nothing
else; every number below is derived here, at read time, from records that were
already on disk. That is what makes it possible to add a statistic later and
have it apply retroactively to every session ever run.

Two properties worth knowing before trusting anything printed here.

The counters are computed over the key RESTRICTED to the first N positions --
never over the whole table. §3.3 is emphatic about this and it is not a detail:
target density is drawn per row, so scoring an unviewed remainder turns random
density into a systematic error that grows with how much of the table was left
alone.

And nothing here regenerates a table. The key and the events are both in the
record, and every counter falls out of those two alone. So a change to the
generator -- a new alphabet, a different draw order -- cannot retroactively
corrupt the scoring of sessions recorded before it.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

RUN_SECONDS = 120.0

# Below this many anchor-measurement pairs, the spread of r is not estimated
# from data and the placeholder cuts of §5.3 apply instead.
MIN_PAIRS = 10
STUB_MODERATE = 0.15
STUB_STRONG = 0.25

# Once there are enough pairs, the cuts come from the observed spread. The
# window keeps the estimate current as the learning effect decays.
WINDOW = 30
Z_MODERATE = 1.0
Z_STRONG = 2.0

# Candidate metrics, and which way is better. The last two are not in the spec:
# §4.3 lists only the first five. They are added because §4.4 chooses the main
# metric from the data rather than from the literature, and §3.1's whole premise
# is that any statistic can be computed retroactively over the stored stream --
# so leaving a plausible discriminator out of the competition would be the one
# choice that cannot be undone later.
#
# The cost is honest and worth stating: seven candidates on a small sample makes
# it likelier that the winner wins by luck. A winner should beat the field by a
# clear margin, not by a hair.
DIRECTION = {
    "A": +1,        # symbols per second
    "T3": +1,       # Whipple accuracy
    "T_exc": +1,    # accuracy on exceptions
    "E": +1,        # N · T3
    "Au": +1,       # workability
    "O_rate": -1,   # false marks per symbol viewed
    "D1_rate": -1,  # exceptions taken for ordinary targets
}
CANDIDATES = tuple(DIRECTION)

# Training counts as closed when the fitted learning curve has this little left
# to climb, as a share of its own limit. Not a finding: 5% is a third of the
# 15% drop the placeholder threshold calls "moderate", on the reasoning that a
# residual bias should be small compared with the smallest effect it could
# disguise. It is a stopping rule, and a stopping rule has to be *some* number.
READY_BIAS_PCT = 5.0


# --------------------------------------------------------------- counters


@dataclass
class Scored:
    session_id: str
    session_index: int
    started_at: str
    trigger: str
    mode: str
    is_anchor: bool
    anchor_id: str
    minutes_worked_before: int
    block_index: int
    presentation: Dict[str, str]

    N: int = 0
    C: int = 0
    n_reg: int = 0
    n_exc: int = 0
    S_reg: int = 0
    S_exc: int = 0
    P_reg: int = 0
    P_exc: int = 0
    D1: int = 0
    D2: int = 0
    O_raw: int = 0
    I: int = 0
    repeats: int = 0

    @property
    def n(self) -> int:
        return self.n_reg + self.n_exc

    @property
    def S(self) -> int:
        return self.S_reg + self.S_exc

    @property
    def P(self) -> int:
        return self.P_reg + self.P_exc

    @property
    def W(self) -> int:
        """Target marked with the wrong button."""
        return self.D1 + self.D2

    @property
    def O(self) -> int:
        """§4.3: D1 and D2 enter O with weight 1 by default.

        They are stored apart so any other weighting can be recomputed from the
        same records; no weight is fitted by eye.
        """
        return self.O_raw + self.W

    def metrics(self) -> Dict[str, Optional[float]]:
        n, S, P, O = self.n, self.S, self.P, self.O
        denominator = S + O + P
        T3 = S / denominator if denominator else None
        return {
            "A": self.N / RUN_SECONDS,
            "T3": T3,
            "T_exc": (self.S_exc / self.n_exc) if self.n_exc else None,
            "E": (self.N * T3) if T3 is not None else None,
            "Au": ((self.N / RUN_SECONDS) * (S - O - P) / n) if n else None,
            # Add-one-half smoothing, because a rested run genuinely produces
            # zero false marks and a ratio against zero is undefined. Jeffreys'
            # prior rather than a fudge: it is the standard correction for a
            # rate estimated from few events.
            "O_rate": ((self.O_raw + 0.5) / (self.N + 1)) if self.N else None,
            "D1_rate": ((self.D1 + 0.5) / (self.n_exc + 1)) if self.n_exc else None,
        }


def score(record: Dict) -> Optional[Scored]:
    stop = record.get("stop_click")
    if not stop:
        return None

    generation = record.get("generation", {})
    cols = int(generation.get("cols", 40))
    scorable = cols - 1

    stop_row, stop_col = int(stop["row"]), int(stop["col"])

    # §3.3, with the spec's "позиция − 1" translated out of 1-based indexing:
    # position p in a row maps to the 0-based column p - 1, and p - 1 positions
    # have been viewed, so the count in the last row is exactly stop_col.
    N = scorable * stop_row + stop_col

    def viewed(row: int, col: int) -> bool:
        return row < stop_row or (row == stop_row and col <= stop_col)

    key = {
        (int(e["row"]), int(e["col"])): e["type"]
        for e in record.get("key", [])
        if viewed(int(e["row"]), int(e["col"]))
    }

    context = record.get("context", {})
    out = Scored(
        session_id=record.get("session_id", "?"),
        session_index=int(record.get("session_index", 0)),
        started_at=record.get("started_at", ""),
        trigger=context.get("trigger", "?"),
        mode=context.get("mode", "?"),
        is_anchor=bool(context.get("is_anchor", False)),
        anchor_id=context.get("anchor_session_id", ""),
        minutes_worked_before=int(context.get("minutes_worked_before", 0)),
        block_index=int(context.get("block_index", 0)),
        presentation=record.get("presentation", {}),
        N=N,
        # The last row is partial, so it counts as viewed only if anything in it
        # was. Used for reporting only -- no metric depends on it.
        C=stop_row + (1 if stop_col >= 1 else 0),
    )

    # First action per cell wins, matching the surface: there is no undo, and a
    # later click on a marked cell is logged but changes nothing.
    acted: Dict[tuple, str] = {}
    for event in record.get("events", []):
        cell = (int(event["row"]), int(event["col"]))
        if cell[1] == 0:
            out.I += 1
            continue
        if cell in acted:
            out.repeats += 1
            continue
        acted[cell] = event["button"]

    for cell, kind in key.items():
        got = acted.get(cell)
        if kind == "exception":
            out.n_exc += 1
            if got is None:
                out.P_exc += 1
            elif got == "right":
                out.S_exc += 1
            else:
                out.D1 += 1
        else:
            out.n_reg += 1
            if got is None:
                out.P_reg += 1
            elif got == "left":
                out.S_reg += 1
            else:
                out.D2 += 1

    for cell in acted:
        if cell not in key and viewed(*cell):
            out.O_raw += 1

    return out


def load_scored() -> List[Scored]:
    scored = [score(record) for record in store.load_all()]
    return [s for s in scored if s is not None]


# ------------------------------------------------------------------ pairs


@dataclass
class Pair:
    session: Scored
    anchor: Scored
    ratios: Dict[str, Optional[float]]


def pairs_for(scored: Sequence[Scored], metric: str) -> List[Pair]:
    """Every non-anchor run against the anchor it points at (§5.2).

    The record carries the anchor's id outright, so nothing here has to infer
    which runs belong together from timestamps -- and a run whose anchor is
    missing is dropped rather than silently compared against the wrong one.
    """
    by_id = {s.session_id: s for s in scored}
    out: List[Pair] = []
    for session in scored:
        if session.is_anchor:
            continue
        anchor = by_id.get(session.anchor_id)
        if anchor is None:
            continue
        ratios = {}
        anchor_metrics = anchor.metrics()
        session_metrics = session.metrics()
        for name in CANDIDATES:
            a, x = anchor_metrics[name], session_metrics[name]
            if a is None or x is None or a == 0 or x == 0:
                ratios[name] = None
                continue
            # r is always oriented so that below 1 means worse. For an error
            # rate that means inverting it, or every threshold downstream would
            # have to know which way each metric points.
            ratios[name] = (x / a) if DIRECTION[name] > 0 else (a / x)
        out.append(Pair(session=session, anchor=anchor, ratios=ratios))
    return out


def thresholds(history: Sequence[float]) -> Dict[str, float]:
    """Cuts on r: from the observed spread once there is one, stubs before.

    The z-multipliers are themselves a convention, not a finding. They should be
    re-derived once there are enough runs to see what a real slump looks like
    against ordinary day-to-day variation; until then they are the least
    arbitrary thing available, which is not the same as being right.
    """
    if len(history) < MIN_PAIRS:
        return {
            "moderate": 1.0 - STUB_MODERATE,
            "strong": 1.0 - STUB_STRONG,
            "source": 0.0,
        }
    window = list(history)[-WINDOW:]
    mu, sigma = mean(window), pstdev(window)
    return {
        "moderate": mu - Z_MODERATE * sigma,
        "strong": mu - Z_STRONG * sigma,
        "source": float(len(window)),
    }


def classify(r: float, cuts: Dict[str, float]) -> str:
    if r < cuts["strong"]:
        return "strong"
    if r < cuts["moderate"]:
        return "moderate"
    return "ok"


# ------------------------------------------------------- metric selection


def discriminability(scored: Sequence[Scored]) -> Dict[str, Optional[float]]:
    """§4.4: d = (mean_fresh − mean_tired) / s_within, per candidate.

    Fresh and tired are split on the observed distribution of
    `minutes_worked_before` -- lowest third against highest third, middle
    discarded -- rather than on a constant. A constant would be a guess about
    where his fatigue sets in, which is the very thing being measured.
    """
    usable = [s for s in scored if s.minutes_worked_before >= 0]
    if len(usable) < 6:
        return {name: None for name in CANDIDATES}

    worked = sorted(s.minutes_worked_before for s in usable)
    low = worked[len(worked) // 3]
    high = worked[(2 * len(worked)) // 3]
    if low == high:
        return {name: None for name in CANDIDATES}

    fresh = [s for s in usable if s.minutes_worked_before <= low]
    tired = [s for s in usable if s.minutes_worked_before >= high]

    out: Dict[str, Optional[float]] = {}
    for name in CANDIDATES:
        a = [s.metrics()[name] for s in fresh]
        b = [s.metrics()[name] for s in tired]
        a = [v for v in a if v is not None]
        b = [v for v in b if v is not None]
        if len(a) < 2 or len(b) < 2:
            out[name] = None
            continue
        # Pooled within-state spread. Population sd, not sample: with groups this
        # small the Bessel correction moves the answer more than the data does.
        pooled = math.sqrt((pstdev(a) ** 2 + pstdev(b) ** 2) / 2)
        if not pooled:
            out[name] = None
            continue
        # Signed by direction, so a larger d always means "separates the two
        # states better", whichever way the metric itself points.
        out[name] = (mean(a) - mean(b)) / pooled * DIRECTION[name]
    return out


# --------------------------------------------------------- learning curve


def learning_curve(scored: Sequence[Scored]) -> Optional[Dict[str, float]]:
    """Fit E_i = E_inf − a·exp(−i/τ) (§5.4), to size the residual bias.

    τ is found by a scan and (E_inf, a) by least squares at each τ. No solver
    dependency: this has to run on a machine where the only guarantee is the
    standard library.
    """
    points = [(s.session_index, s.metrics()["E"]) for s in scored]
    points = [(i, v) for i, v in points if v is not None]
    if len(points) < 10:
        return None

    xs = [float(i) for i, _ in points]
    ys = [float(v) for _, v in points]

    best = None
    for step in range(1, 601):
        tau = step / 10.0
        zs = [math.exp(-x / tau) for x in xs]
        n = len(xs)
        mean_z, mean_y = sum(zs) / n, sum(ys) / n
        var_z = sum((z - mean_z) ** 2 for z in zs)
        if var_z <= 0:
            continue
        slope = sum((z - mean_z) * (y - mean_y) for z, y in zip(zs, ys)) / var_z
        intercept = mean_y - slope * mean_z
        residual = sum((y - (intercept + slope * z)) ** 2 for z, y in zip(zs, ys))
        if best is None or residual < best[0]:
            # y = intercept + slope·z, and the model is y = E_inf − a·exp(−i/τ),
            # so a = −slope.
            best = (residual, tau, intercept, -slope)

    if best is None:
        return None
    residual, tau, e_inf, a = best
    total = sum((y - mean(ys)) ** 2 for y in ys)
    return {
        "tau": tau,
        "E_inf": e_inf,
        "a": a,
        "r2": (1 - residual / total) if total else 0.0,
        "n": float(len(points)),
    }


# ---------------------------------------------------------------- verdict


def verdict(session_id: str, include_ratio: bool = True) -> Optional[str]:
    """One line for the notification after an operational run, or nothing.

    Nothing is the common case and the correct one: training mode shows no
    verdict at all (§5.1), and an anchor has nothing to be compared against.
    """
    scored = load_scored()
    current = next((s for s in scored if s.session_id == session_id), None)
    if current is None or current.mode != "operational" or current.is_anchor:
        return None

    metric = main_metric(scored)
    pairs = pairs_for(scored, metric)
    here = next((p for p in pairs if p.session.session_id == session_id), None)
    if here is None or here.ratios[metric] is None:
        return None

    r = here.ratios[metric]
    history = [
        p.ratios[metric]
        for p in pairs
        if p.ratios[metric] is not None
        and p.session.session_index < current.session_index
    ]
    cuts = thresholds(history)
    verdict_now = classify(r, cuts)

    drop = f"{metric} = {r:.2f} от якоря" if include_ratio else "Вердикт"

    if len(history) < MIN_PAIRS:
        # §5.3: below ten pairs the value is shown without a verdict, plus a
        # flag when the drop clears the placeholder. Saying more than the data
        # supports is how a tool like this loses its credibility on week one.
        note = f"{drop}. Пар пока {len(history)}, порога нет."
        if r < 1.0 - STUB_MODERATE:
            note += f" Падение больше {int(STUB_MODERATE * 100)}% — заглушка."
        return note

    # §6: a single reading below the cut is not grounds. The rule fires on two
    # consecutive measurements, so an isolated bad run cannot end the day.
    previous = [
        p for p in pairs
        if p.ratios[metric] is not None
        and p.session.session_index < current.session_index
    ]
    previous_bad = bool(previous) and classify(
        previous[-1].ratios[metric], thresholds(history[:-1])
    ) != "ok"

    if verdict_now == "ok":
        return f"{drop}. В норме."
    if not previous_bad:
        return f"{drop}. Ниже порога, но однократно — не основание."
    if verdict_now == "strong":
        return f"{drop}. Второй замер подряд, падение сильное: блоки на сегодня прекратить."
    return f"{drop}. Второй замер подряд: удлинить перерыв."


# --------------------------------------------------------------- after a run


def _pct(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    if value is None or reference in (None, 0):
        return None
    return (value / reference - 1.0) * 100.0


def _compare(now: Dict[str, Optional[float]],
             reference: Dict[str, Optional[float]]) -> str:
    """Speed and accuracy shown side by side, never one without the other.

    A single composite invites optimising the composite, and a bare speed figure
    invites going faster. Putting the two next to each other makes a deliberate
    push self-refuting: the gain shows up in one column and the cost in the one
    beside it, in the same glance.
    """
    parts = []
    for name, label in (("E", "E"), ("A", "скорость"), ("T3", "точность")):
        delta = _pct(now[name], reference[name])
        parts.append(f"{label} {delta:+.0f}%" if delta is not None else f"{label} —")
    return "   ".join(parts)


def _reading(current: "Scored", reference: "Scored") -> List[str]:
    """Plain-language notes on what changed, in order of diagnostic weight.

    Descriptive, never prescriptive: a recommendation is a verdict, and §5.1
    holds verdicts back to operational mode. These say what the numbers did; the
    verdict, when it exists, says what to do about it.

    The order matters. Speed alone is nearly useless -- fatigue on a cancellation
    task usually shows as the SAME or HIGHER speed with worse accuracy, so
    "am I slower?" is the one question that reliably misses it. The trade shows
    first, then loss of response control, then the conditional rule, then
    detection.
    """
    now, before = current.metrics(), reference.metrics()
    notes: List[str] = []

    d_speed = _pct(now["A"], before["A"])
    d_accuracy = _pct(now["T3"], before["T3"])
    d_product = _pct(now["E"], before["E"])

    if d_speed is not None and d_accuracy is not None:
        if d_speed >= 5 and d_accuracy <= -5:
            notes.append("Разгон за счёт точности — размен, который обычно и "
                         "означает утомление. Скорость тут ничего не выдаёт.")
        elif d_speed <= -5 and d_accuracy >= 5:
            notes.append("Медленнее и точнее — так выглядит осознанное "
                         "замедление, не спад.")
        elif d_speed <= -5 and d_accuracy <= -5:
            notes.append("Хуже по обеим осям сразу — размена нет, "
                         "просело всё.")

    # False marks are the strongest single sign: marking a cell that holds no
    # target at all is a failure of response control, not of vision.
    if current.O_raw > 0 and reference.O_raw == 0:
        notes.append(f"Ложных отметок {current.O_raw} против нуля у якоря — "
                     f"отмечаешь, не досмотрев.")

    # The conditional rule is the part that goes first under speed pressure:
    # checking the left neighbour is an extra operation and it is the one that
    # gets dropped.
    if reference.D1 > 0 and current.D1 >= 2 * reference.D1 and current.D1 >= 4:
        notes.append(f"Ошибок на исключениях {reference.D1} → {current.D1}: "
                     f"проверка соседа слева пропускается чаще.")
    elif reference.D1 == 0 and current.D1 >= 3:
        notes.append(f"Ошибок на исключениях {current.D1} против нуля у якоря.")

    if current.P - reference.P >= 5:
        notes.append(f"Пропусков {reference.P} → {current.P}: цель хуже "
                     f"выскакивает из фона.")

    if not notes:
        if d_product is not None and d_product >= 10:
            notes.append("Выше якоря без размена — просто лучше.")
        elif d_product is not None and d_product <= -10:
            notes.append("Ниже якоря, но без явного размена и без всплеска "
                         "ошибок — вероятнее шум, чем спад.")
        else:
            notes.append("С якорем расхождений нет.")

    return notes


def after_run(session_id: str) -> Optional[str]:
    """The line shown once a run is finished. Numbers always; a verdict only
    where §5.1 allows one."""
    scored = load_scored()
    by_id = {s.session_id: s for s in scored}
    current = by_id.get(session_id)
    if current is None:
        return None

    now = current.metrics()

    def fmt(name: str, spec: str) -> str:
        value = now[name]
        return format(value, spec) if value is not None else "—"

    lines = [
        f"N {current.N} · скорость {fmt('A', '.2f')}/с · "
        f"точность {fmt('T3', '.3f')} · E {fmt('E', '.0f')}",
        f"пропущено {current.P} · не то действие {current.W} · "
        f"ложных {current.O_raw}",
    ]

    anchor = by_id.get(current.anchor_id)
    if current.is_anchor or anchor is None or anchor.session_id == session_id:
        lines.append("Это якорь — с ним сравнятся следующие прогоны.")
    else:
        lines.append("к якорю:    " + _compare(now, anchor.metrics()))

    earlier = [s for s in scored if s.session_index < current.session_index]
    if earlier:
        lines.append("к прошлому: " + _compare(now, earlier[-1].metrics()))

    if anchor is not None and anchor.session_id != session_id:
        lines.append("")
        lines.extend(_reading(current, anchor))

    decision = verdict(session_id, include_ratio=False)
    if decision:
        lines.append("")
        lines.append(decision)

    return "\n".join(lines)


def main_metric(scored: Sequence[Scored]) -> str:
    """The candidate with the largest d, or E until there is enough to choose.

    §4.4 refuses to fix the main metric in advance, and the fallback has to be
    something: E = N·T3 is the one candidate that moves with both speed and
    accuracy, so a composite with invented weights is not smuggled in by the
    back door.
    """
    scores = discriminability(scored)
    ranked = [(v, k) for k, v in scores.items() if v is not None]
    if not ranked:
        return "E"
    return max(ranked)[1]


# ----------------------------------------------------------------- legend

LEGEND = """
ЧТО ЗДЕСЬ СЧИТАЕТСЯ

Все счётчики берутся ТОЛЬКО по просмотренной части — до клика остановки.
Плотность целей разыгрывается для каждой строки заново, поэтому счёт по всей
таблице превратил бы случайную плотность в систематическую ошибку, растущую с
размером непросмотренного остатка.

Счётчики
  N       просмотрено символов: 39 на каждую полную строку плюс позиция клика
          остановки. Первая клетка строки — буква-инструкция, в счёт не входит.
  C       просмотрено строк.
  n       целей в просмотренной части; в скобках — сколько из них исключений,
          то есть целей с «А» слева.
  S       взято правильным действием: ЛКМ на обычной цели, ПКМ на исключении.
  P       пропущено полностью — цель осталась без отметки.
  D1      вычеркнул там, где надо было подчеркнуть: исключение принято за
          обычную цель, то есть «А» слева не замечена.
  D2      подчеркнул там, где надо было вычеркнуть: правило применено лишний раз.
  O       отмечен не-целевой символ. В таблице показано это чистое число; в
          метриках O = ложные отметки + D1 + D2 (§4.3, вес 1 у каждой).
  I       клики по букве-инструкции. Отметку не оставляют, только пишутся в лог.

  Тождество: n = S + P + D1 + D2. Спецификация пишет n = S + P — это верно лишь
  когда ни одна цель не отмечена не той кнопкой.

Метрики, t = 120 с
  A       = N / t                    скорость, символов в секунду
  T3      = S / (S + O + P)          точность по Уипплу: доля правильно взятых
                                     целей среди всего, что было сделано, зря
                                     отмечено или упущено. Не растёт от
                                     беспорядочных отметок — из-за этого
                                     отвергнут T1 = M/n, который растёт.
  T_exc   = S_exc / n_exc            точность на исключениях
  E       = N · T3                   продуктивность. Единственный кандидат,
                                     который двигается и от скорости, и от
                                     точности сразу.
  Au      = (N/t)·(S − O − P)/n      работоспособность. Уходит в минус, когда
                                     ошибок больше, чем попаданий.
  O_rate  = (O + ½) / (N + 1)        частота ложных отметок
  D1_rate = (D1 + ½) / (n_exc + 1)   частота отказов правила исключения

  Последние два в спецификации не перечислены. Добавлены потому, что §4.4
  выбирает главную метрику по данным, а не по литературе, и оставить
  правдоподобного кандидата вне соревнования — единственное решение, которое
  потом не отыграть. У них «меньше — лучше», поэтому в паре с якорем берётся
  обратное отношение: r ниже единицы означает «хуже» для всех метрик без
  исключения. Прибавка ½ нужна, потому что в бодром прогоне ложных отметок
  честно ноль, а отношение к нулю не определено.

  Цена честная: семь кандидатов на малой выборке повышают шанс, что победитель
  победил случайно. Побеждать он должен с заметным отрывом, а не на волосок.

Парная схема
  Якорь — первый прогон после перерыва в 6 часов и больше. Всё остальное в этой
  группе сравнивается с ним: r = x_текущий / x_якорь. r ниже единицы — падение.
  Схема самонормирующаяся: якорь забирает на себя всё, что различается между
  днями — сон, время суток, самочувствие. Внешняя норма не нужна.

  Порог. Пока пар меньше 10 — заглушка: умеренное падение больше 15%, сильное
  больше 25%. От 10 пар порог считается по фактическому разбросу r в окне из
  последних 30 пар: умеренное ниже μ − 1σ, сильное ниже μ − 2σ. Множители 1 и 2
  — соглашение, а не результат; их надо будет пересчитать, когда станет видно,
  как настоящий спад выглядит на фоне обычного дневного разброса.

  Одиночное значение ниже порога вердикта не даёт. Правило срабатывает только по
  двум последовательным замерам.

Выбор главной метрики
  d = (среднее у свежего − среднее у уставшего) / разброс внутри состояния.
  «Свежий» и «уставший» — нижняя и верхняя трети распределения наработанных
  минут на момент прогона, средняя треть отброшена. Границы берутся из самих
  данных, а не константой: константа была бы догадкой о том, когда наступает
  твоё утомление, а это ровно то, что измеряется. Главной становится метрика с
  наибольшим d — то есть та, что лучше всех различает два состояния.

Как это читать
  Скорость сама по себе не признак. На корректурной пробе утомление обычно
  выглядит как та же или ВЫШЕ скорость при худшей точности, поэтому вопрос
  «я стал медленнее?» — единственный, который надёжно промахивается.

  Признаки, в порядке диагностического веса:
  1. Размен. Скорость вверх, точность вниз. Это утомление, а не улучшение.
  2. Ложные отметки O там, где у якоря их не было. Отмечена клетка, в которой
     цели нет вообще — отказ контроля ответа, не зрения. Самый сильный
     одиночный признак.
  3. Рост D1. Проверка соседа слева — лишняя операция, и под скоростным
     давлением бросают именно её.
  4. Рост P. Цель перестаёт выскакивать из фона — это уже про сканирование.
  Один признак — наблюдение. Два вместе, дважды подряд — основание.

  Пока идёт training, вердикта нет, но полезно помнить: научение тянет
  результат вверх, поэтому ПАДЕНИЕ на этом фоне весомее, чем то же падение
  было бы на плато. Рост же не значит почти ничего — он ожидается.

Кривая научения
  E_i = E_inf − a·exp(−i/τ), где i — номер сессии. E_inf — предел, к которому
  идёт продуктивность; a — насколько первая сессия ниже предела; τ — за сколько
  сессий отставание падает в e раз. Считается от десяти сессий. Пока освоение
  продолжается, оно завышает r внутри дня, то есть вердикт скорее пропустит
  настоящий спад, чем поднимет ложную тревогу.
""".strip("\n")


# ----------------------------------------------------------------- report


def report(with_legend: bool = True) -> None:
    scored = load_scored()
    if not scored:
        print("Записей нет.")
        return

    print(f"Сессий: {len(scored)}"
          f"   training: {sum(1 for s in scored if s.mode == 'training')}"
          f"   operational: {sum(1 for s in scored if s.mode == 'operational')}")

    looks = {tuple(sorted(s.presentation.items())) for s in scored}
    if len(looks) > 1:
        print(f"ВНИМАНИЕ: условий предъявления {len(looks)} — сессии с разными "
              f"условиями между собой несопоставимы.")

    # Header and rows are built from ONE column spec. They used to be two
    # independent format strings and drifted apart the moment a width changed.
    columns = [
        ("#", 3), ("время", 11), ("триггер", 9), ("якорь", 5),
        ("N", 4), ("C", 3), ("n", 3), ("искл", 4),
        ("S", 3), ("P", 3), ("O", 3), ("D1", 3), ("D2", 3), ("I", 3),
        ("T3", 5), ("T_exc", 5), ("E", 5), ("A", 5),
    ]

    def row(values):
        return " ".join(
            str(value).rjust(width) for (_, width), value in zip(columns, values)
        )

    print()
    print(row([name for name, _ in columns]))
    for s in scored:
        m = s.metrics()

        def num(name, spec):
            value = m[name]
            return format(value, spec) if value is not None else "--"

        print(row([
            s.session_index,
            s.started_at[5:16].replace("T", " "),
            s.trigger,
            "да" if s.is_anchor else "",
            s.N, s.C, s.n, s.n_exc,
            s.S, s.P, s.O_raw, s.D1, s.D2, s.I,
            num("T3", ".3f"), num("T_exc", ".3f"),
            num("E", ".0f"), num("A", ".2f"),
        ]))

    metric = main_metric(scored)
    scores = discriminability(scored)
    print()
    if all(v is None for v in scores.values()):
        print(f"Главная метрика не выбрана — данных мало. Пока считаю по {metric}.")
    else:
        print("Различающая способность d = (свежий − уставший) / s внутри состояния:")
        for name in CANDIDATES:
            value = scores[name]
            flag = "  <- главная" if name == metric else ""
            print(f"   {name:<6} {format(value, '+.3f') if value is not None else '  --'}{flag}")

    pairs = pairs_for(scored, metric)
    usable = [p for p in pairs if p.ratios[metric] is not None]
    print()
    print(f"Пар «якорь — замер» по {metric}: {len(usable)}"
          f" (порог по данным с {MIN_PAIRS})")
    if usable:
        history: List[float] = []
        for pair in usable:
            r = pair.ratios[metric]
            cuts = thresholds(history)
            state = classify(r, cuts)
            label = {"ok": "норма", "moderate": "умеренное",
                     "strong": "сильное"}[state]
            basis = "заглушка" if cuts["source"] == 0 else f"по {int(cuts['source'])} парам"
            print(f"   #{pair.session.session_index:<3d} r = {r:.3f}"
                  f"   порог {cuts['moderate']:.3f} ({basis})   {label}")
            history.append(r)

    curve = learning_curve(scored)
    print()
    if curve is None:
        print("Кривая научения: нужно не меньше 10 сессий.")
    else:
        print(f"Кривая научения E_i = E_inf − a·exp(−i/τ):")
        print(f"   E_inf = {curve['E_inf']:.0f}   a = {curve['a']:.0f}"
              f"   τ = {curve['tau']:.1f}   R² = {curve['r2']:.3f}"
              f"   по {int(curve['n'])} сессиям")
        remaining = curve["a"] * math.exp(-max(s.session_index for s in scored)
                                          / curve["tau"])
        share = remaining / curve["E_inf"] * 100 if curve["E_inf"] else 0.0
        print(f"   остаточное смещение на следующей сессии ≈ {remaining:.0f} "
              f"единиц E ({share:.1f}% от предела)")

    # Readiness to leave training. Announced, never acted on: §5.2 wants an
    # explicit command, and switching by itself would be deciding for him that
    # the training phase is closed.
    if any(s.mode == "training" for s in scored):
        print()
        if curve is None:
            need = 10 - len(scored)
            print(f"Тренировка: до оценки кривой научения не хватает "
                  f"{max(0, need)} сессий.")
        elif share <= READY_BIAS_PCT:
            print(f"Тренировка закрыта: остаточное смещение {share:.1f}% при "
                  f"пороге {READY_BIAS_PCT}%.")
            print("   Можно переключаться:  study attention mode operational")
        else:
            print(f"Тренировка продолжается: остаточное смещение {share:.1f}%, "
                  f"нужно {READY_BIAS_PCT}% или меньше.")

    if with_legend:
        print()
        print(LEGEND)


# --------------------------------------------------------------- selftest


SYNTHETIC = {
    "session_id": "selftest",
    "session_index": 0,
    "started_at": "2026-01-01T00:00:00+00:00",
    "context": {"trigger": "manual", "mode": "training", "is_anchor": True,
                "anchor_session_id": "selftest", "minutes_worked_before": 0,
                "block_index": 0},
    "generation": {"rows": 80, "cols": 40, "seed": 0},
    "presentation": {},
    "key": [
        {"row": 0, "col": 3, "type": "target"},
        {"row": 0, "col": 10, "type": "exception"},
        {"row": 1, "col": 2, "type": "target"},
        {"row": 2, "col": 4, "type": "exception"},
        # Beyond the stop click: present in the table, outside the viewed span,
        # and therefore invisible to every counter. This row is the whole point
        # of the test -- scoring these would be the §3.3 error.
        {"row": 2, "col": 20, "type": "target"},
        {"row": 3, "col": 1, "type": "target"},
    ],
    "events": [
        {"t_ms": 100, "row": 0, "col": 3, "button": "left"},    # S_reg
        {"t_ms": 200, "row": 0, "col": 10, "button": "left"},   # D1
        {"t_ms": 300, "row": 1, "col": 2, "button": "right"},   # D2
        {"t_ms": 400, "row": 2, "col": 4, "button": "right"},   # S_exc
        {"t_ms": 500, "row": 0, "col": 5, "button": "left"},    # O_raw
        {"t_ms": 600, "row": 2, "col": 30, "button": "left"},   # unviewed, ignored
        {"t_ms": 700, "row": 0, "col": 0, "button": "left"},    # I
        {"t_ms": 800, "row": 0, "col": 3, "button": "right"},   # repeat, no effect
    ],
    "stop_click": {"row": 2, "col": 5},
}

EXPECTED = {
    "N": 83, "C": 3,
    "n_reg": 2, "n_exc": 2, "S_reg": 1, "S_exc": 1,
    "P_reg": 0, "P_exc": 0, "D1": 1, "D2": 1,
    "O_raw": 1, "I": 1, "repeats": 1,
}


def selftest() -> int:
    got = score(SYNTHETIC)
    assert got is not None

    failures = []
    for name, want in EXPECTED.items():
        have = getattr(got, name)
        mark = "ok" if have == want else "ПРОВАЛ"
        if have != want:
            failures.append(f"{name}: ждали {want}, получили {have}")
        print(f"  {name:<8} {have:>4}   ждали {want:>4}   {mark}")

    # n = S + P + W. The spec writes n = S + P (§4.1), which holds only when no
    # target was marked with the wrong button. D1 and D2 are stored separately
    # by design, so the three-way split is made explicit here rather than left
    # to collapse silently into one of the other terms.
    if got.n != got.S + got.P + got.W:
        failures.append(f"тождество n = S + P + W: {got.n} != "
                        f"{got.S} + {got.P} + {got.W}")

    metrics = got.metrics()
    checks = {
        "T3": 2 / 5,          # S / (S + O + P), O = O_raw + D1 + D2 = 3
        "T_exc": 1 / 2,
        "A": 83 / 120,
        "E": 83 * (2 / 5),
        "Au": (83 / 120) * (2 - 3 - 0) / 4,
    }
    print()
    for name, want in checks.items():
        have = metrics[name]
        ok = have is not None and abs(have - want) < 1e-9
        if not ok:
            failures.append(f"{name}: ждали {want}, получили {have}")
        print(f"  {name:<8} {have if have is None else round(have, 6):>10}"
              f"   ждали {round(want, 6):>10}   {'ok' if ok else 'ПРОВАЛ'}")

    print()
    if failures:
        for line in failures:
            print("  " + line)
        print(f"ПРОВАЛЕНО: {len(failures)}")
        return 1
    print("Все счётчики и метрики сходятся с ручным расчётом.")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "report":
        # `report short` for the numbers alone -- the legend is long, and once
        # its definitions are known it is in the way.
        report(with_legend=(len(sys.argv) < 3 or sys.argv[2] != "short"))
        return 0
    if command == "after":
        if len(sys.argv) < 3:
            print("after <session_id>", file=sys.stderr)
            return 1
        line = after_run(sys.argv[2])
        print(line if line else "(нет данных)")
        return 0
    if command == "legend":
        print(LEGEND)
        return 0
    if command == "selftest":
        return selftest()
    if command == "verdict":
        if len(sys.argv) < 3:
            print("verdict <session_id>", file=sys.stderr)
            return 1
        line = verdict(sys.argv[2])
        print(line if line else "(нет вердикта)")
        return 0
    print(f"Неизвестная команда: {command}", file=sys.stderr)
    print("report [short] | legend | selftest | after <id> | verdict <id>",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
