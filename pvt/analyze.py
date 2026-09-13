#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Analysis over the accumulated PVT records.

Everything here is derived at read time from records that were already on disk,
so a statistic added later applies retroactively to every session ever run.

Three lessons from the cancellation test this replaces are built in from the
start rather than discovered again:

  * A metric is only offered for the anchor ratio if a ratio of it means
    anything. Lapse counts run 0-15; a ratio of two such counts swings
    thirtyfold and produced thresholds that could never fire. Counts enter the
    metric competition, never the pair scheme.
  * The main metric can come out "not chosen". Taking max(d) over a list always
    returns something, and last time it crowned a coin flip at d = 0.07.
  * Records are pooled only within one protocol version. Reaction time depends
    strongly on foreperiod length, so a change to the interval makes old and new
    runs measurements of different things.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from statistics import mean, median, pstdev
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

# The nominal centre of the interval distribution. Foreperiod correction reports
# each session at this value, so sessions are compared at the same point on the
# preparation curve rather than at wherever their own draw happened to land.
ISI_CENTRE_MS = (store.ISI_MIN_MS + store.ISI_MAX_MS) / 2

# Rested lapse rate the personal threshold aims for. Not a finding: with ~108
# trials a rate near 10% gives a count around 11 whose standard error is about
# 3.3, i.e. 30% relative -- usable. At the 1% a very fast subject produces under
# the published threshold, the count is 1 and carries no information at all.
TARGET_LAPSE_RATE = 0.10

MIN_PAIRS = 10
STUB_MODERATE = 0.15
STUB_STRONG = 0.25
WINDOW = 30
Z_MODERATE = 1.0
Z_STRONG = 2.0

# A candidate has to clear this |d| to be considered a discriminator at all, and
# beat the runner-up by this margin to be declared the main metric. Both are
# conventions; their purpose is to make "nothing qualifies" an available answer.
MIN_D = 0.5
MIN_MARGIN = 0.2

# Share of trials on one device above which a session counts as that device,
# with the stray minority trials dropped rather than the whole session.
DOMINANT_SHARE = 0.95

DIRECTION = {
    "inv_rt": +1,          # mean 1/RT, the literature's primary measure
    "inv_rt_adj": +1,      # the same, corrected for foreperiod
    "slowest10": +1,       # mean 1/RT of the slowest tenth -- where lapses live
    "fastest10_rt": -1,    # mean RT of the fastest tenth -- best-case capacity
    "median_rt": -1,
    "lapses": -1,
    "false_starts": -1,
}
CANDIDATES = tuple(DIRECTION)

# Metrics whose anchor ratio is meaningful. Counts are excluded on purpose: see
# the module docstring.
RATIOABLE = ("inv_rt", "inv_rt_adj", "slowest10", "fastest10_rt", "median_rt")


# --------------------------------------------------------------- regression


def _ols(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float, float]:
    """Least squares fit y = a + b·x. Returns (a, b, residual sd)."""
    n = len(xs)
    if n < 3:
        return (mean(ys) if ys else 0.0), 0.0, (pstdev(ys) if n > 1 else 0.0)
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return my, 0.0, pstdev(ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    return a, b, pstdev(resid)


# ------------------------------------------------------------------ scoring


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
    local_hour: int
    hours_since_previous: Optional[float]
    protocol_version: int
    timing: Dict[str, float]
    # "key", "mouse", or "mixed". The devices are never pooled. The literature's
    # expectation is that a mouse click costs 20-30 ms over a key press; measured
    # here it is the other way round and much larger -- median 186 ms on the
    # mouse against 251 ms on the spacebar, a 65 ms gap, plausibly the mouse's
    # far higher polling rate plus a hand position that made the spacebar
    # awkward. Either way the gap is several times any fatigue effect being
    # looked for, which is the whole reason for keeping the pools apart.
    modality: str = "key"
    # Trials dropped because they used the minority device in a session that is
    # otherwise homogeneous.
    n_off_device: int = 0
    # Karolinska Sleepiness Scale, 1 alert … 9 fighting sleep. None when skipped.
    kss: Optional[int] = None
    # Trials thrown out because the window lost focus during them. Hyprland does
    # not deliver the click that refocuses a window, so the response is
    # swallowed and the next one carries the detour -- which is what produced a
    # 1574 ms "reaction" in the first run.
    n_focus_lost: int = 0

    rts: List[float] = field(default_factory=list)        # valid only
    n_trials: int = 0
    n_false_starts: int = 0
    n_no_response: int = 0
    isis: List[int] = field(default_factory=list)         # aligned with rts

    # Filled by metrics(): the foreperiod fit, kept for reporting.
    fp_slope: float = 0.0
    fp_resid_sd: float = 0.0

    def metrics(self, lapse_ms: float) -> Dict[str, Optional[float]]:
        if not self.rts:
            return {name: None for name in CANDIDATES}

        inv = [1000.0 / x for x in self.rts]
        ordered = sorted(self.rts)
        tenth = max(1, len(ordered) // 10)

        # The slowest tenth is taken in RT and reported as 1/RT so that, like
        # every other speed measure here, larger means better.
        slowest = ordered[-tenth:]
        fastest = ordered[:tenth]

        # Foreperiod correction. The predictor is log(ISI) rather than ISI: the
        # preparation effect decelerates -- measured here, RT falls 38 ms over
        # the first 750 ms of interval and only 17 ms over the next 1500 -- which
        # is what a hazard-function account predicts and a straight line does not.
        logs = [math.log(x) for x in self.isis]
        a, b, resid_sd = _ols(logs, inv)
        self.fp_slope = b
        self.fp_resid_sd = resid_sd
        adjusted = a + b * math.log(ISI_CENTRE_MS)

        return {
            "inv_rt": mean(inv),
            "inv_rt_adj": adjusted,
            "slowest10": mean(1000.0 / x for x in slowest),
            "fastest10_rt": mean(fastest),
            "median_rt": median(self.rts),
            "lapses": float(sum(1 for x in self.rts if x >= lapse_ms)),
            "false_starts": float(self.n_false_starts),
        }

    def lapse_rate(self, lapse_ms: float) -> Optional[float]:
        if not self.rts:
            return None
        return sum(1 for x in self.rts if x >= lapse_ms) / len(self.rts)


def score(record: Dict) -> Optional[Scored]:
    trials = record.get("trials") or []
    if not trials:
        return None

    protocol = record.get("protocol", {})
    context = record.get("context", {})
    valid_min = float(protocol.get("valid_rt_min_ms", store.VALID_RT_MIN_MS))

    out = Scored(
        session_id=record.get("session_id", "?"),
        session_index=int(record.get("session_index", 0)),
        started_at=record.get("started_at", ""),
        trigger=context.get("trigger", "?"),
        mode=context.get("mode", "?"),
        is_anchor=bool(context.get("is_anchor", False)),
        anchor_id=context.get("anchor_session_id", ""),
        minutes_worked_before=int(context.get("minutes_worked_before", 0)),
        local_hour=int(context.get("local_hour", 0)),
        hours_since_previous=context.get("hours_since_previous"),
        protocol_version=int(protocol.get("version", 0)),
        timing=record.get("timing", {}) or {},
        n_trials=len(trials),
        kss=(record.get("subjective") or {}).get("value"),
    )

    kinds = set()
    for trial in trials:
        # Older records predate both fields; absent means "fine", which is what
        # they were.
        if not trial.get("focus_ok", True):
            out.n_focus_lost += 1
            continue

        # A press before the stimulus and a press too fast to be a reaction are
        # the same error of commission, counted together as the protocol does.
        out.n_false_starts += len(trial.get("false_starts") or [])
        rt = trial.get("rt_ms")
        if rt is None:
            out.n_no_response += 1
            continue
        if rt < valid_min:
            out.n_false_starts += 1
            continue
        out.rts.append(float(rt))
        out.isis.append(int(trial.get("isi_ms", ISI_CENTRE_MS)))
        kinds.add(trial.get("response", "key"))

    # A session is treated as one device when it is overwhelmingly one device:
    # a few stray presses -- three keys among a hundred clicks -- should not
    # discard a hundred good trials. Below that share it really is two
    # populations and the session is set aside whole.
    if len(kinds) <= 1:
        out.modality = kinds.pop() if kinds else "key"
    else:
        counts: Dict[str, int] = {}
        for trial in trials:
            if trial.get("focus_ok", True):
                rt = trial.get("rt_ms")
                if rt is not None and rt >= valid_min:
                    kind = trial.get("response", "key")
                    counts[kind] = counts.get(kind, 0) + 1
        total = sum(counts.values())
        dominant = max(counts, key=lambda k: counts[k])
        if total and counts[dominant] / total >= DOMINANT_SHARE:
            out.modality = dominant
            keep_rts, keep_isis = [], []
            index = 0
            for trial in trials:
                if not trial.get("focus_ok", True):
                    continue
                rt = trial.get("rt_ms")
                if rt is None or rt < valid_min:
                    continue
                if trial.get("response", "key") == dominant:
                    keep_rts.append(out.rts[index])
                    keep_isis.append(out.isis[index])
                else:
                    out.n_off_device += 1
                index += 1
            out.rts, out.isis = keep_rts, keep_isis
        else:
            out.modality = "mixed"
    return out


def load_scored(version: Optional[int] = None,
                modality: Optional[str] = None) -> List[Scored]:
    """Only what is comparable: one protocol version, one response device.

    The modality defaults to whatever the most recent session used, so switching
    device switches the pool rather than silently mixing two populations of
    reaction times that differ by more than most fatigue effects.
    """
    scored = [score(r) for r in store.load_all()]
    scored = [s for s in scored if s is not None and s.rts]
    if version is None:
        version = store.PROTOCOL_VERSION
    scored = [s for s in scored if s.protocol_version == version]
    if modality is None:
        # A mixed session is not a device and must never define the pool -- it
        # would then select exactly the sessions that were meant to be excluded.
        usable = [s for s in scored if s.modality != "mixed"]
        modality = usable[-1].modality if usable else "key"
    return [s for s in scored if s.modality == modality]


# ------------------------------------------------------- lapse threshold


def personal_lapse_ms(scored: Sequence[Scored]) -> Optional[float]:
    """A cut that puts the rested lapse rate near TARGET_LAPSE_RATE.

    Derived from anchor runs only -- an anchor is meant to be the rested end of
    the range, so a threshold fitted to it leaves room for the count to rise.
    Fitting on everything would place the cut in the middle of the distribution
    the measure is supposed to detect movement in.
    """
    pool: List[float] = []
    for s in scored:
        if s.is_anchor:
            pool.extend(s.rts)
    if len(pool) < 60:
        return None
    ordered = sorted(pool)
    index = int(len(ordered) * (1.0 - TARGET_LAPSE_RATE))
    return float(ordered[min(index, len(ordered) - 1)])


def lapse_threshold(scored: Sequence[Scored]) -> Tuple[float, str]:
    personal = personal_lapse_ms(scored)
    if personal is None:
        return float(store.LAPSE_MS_NOMINAL), "стандарт"
    return round(personal), "по своим данным"


# ------------------------------------------------------------------ pairs


@dataclass
class Pair:
    session: Scored
    anchor: Scored
    ratios: Dict[str, Optional[float]]


def pairs_for(scored: Sequence[Scored], lapse_ms: float) -> List[Pair]:
    by_id = {s.session_id: s for s in scored}
    out: List[Pair] = []
    for session in scored:
        if session.is_anchor:
            continue
        anchor = by_id.get(session.anchor_id)
        if anchor is None:
            continue
        am, sm = anchor.metrics(lapse_ms), session.metrics(lapse_ms)
        ratios: Dict[str, Optional[float]] = {}
        for name in RATIOABLE:
            a, x = am[name], sm[name]
            if a is None or x is None or a == 0 or x == 0:
                ratios[name] = None
                continue
            # Oriented so that below 1 always means worse, whichever way the
            # metric itself points.
            ratios[name] = (x / a) if DIRECTION[name] > 0 else (a / x)
        out.append(Pair(session=session, anchor=anchor, ratios=ratios))
    return out


def thresholds(history: Sequence[float]) -> Dict[str, float]:
    if len(history) < MIN_PAIRS:
        return {"moderate": 1.0 - STUB_MODERATE,
                "strong": 1.0 - STUB_STRONG, "source": 0.0}
    window = list(history)[-WINDOW:]
    mu, sigma = mean(window), pstdev(window)
    return {"moderate": mu - Z_MODERATE * sigma,
            "strong": mu - Z_STRONG * sigma, "source": float(len(window))}


def classify(r: float, cuts: Dict[str, float]) -> str:
    if r < cuts["strong"]:
        return "strong"
    if r < cuts["moderate"]:
        return "moderate"
    return "ok"


# --------------------------------------------------- subjective agreement


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Rank correlation. KSS is ordinal -- its steps are not equal intervals --
    so ranks are the honest treatment; Pearson would assume a metric the scale
    does not claim to have."""
    n = len(xs)
    if n < 4:
        return None

    def rank(values: Sequence[float]) -> List[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            shared = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = shared
            i = j + 1
        return out

    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx)
                    * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def kss_agreement(scored: Sequence[Scored],
                  lapse_ms: float) -> Dict[str, Optional[float]]:
    """How well each candidate tracks his own sense of sleepiness.

    This is a far cheaper selection criterion than `d`. The fresh/tired split
    needs dozens of sessions and was confounded with practice last time; a rating
    labels every single session, so a correlation over a dozen runs already says
    something. It is not ground truth -- the whole reason this tool exists is
    that self-assessment is unreliable -- but it is an axis *independent of the
    measurement*, which is what was missing.

    Signs are corrected so that positive always means "moves with sleepiness as
    expected": KSS rises with sleepiness, and a metric where larger is better
    should fall.
    """
    rated = [s for s in scored if s.kss is not None]
    out: Dict[str, Optional[float]] = {name: None for name in CANDIDATES}
    if len(rated) < 4:
        return out
    kss = [float(s.kss) for s in rated]
    for name in CANDIDATES:
        values = [s.metrics(lapse_ms)[name] for s in rated]
        if any(v is None for v in values):
            continue
        rho = _spearman(kss, values)  # type: ignore[arg-type]
        out[name] = None if rho is None else -rho * DIRECTION[name]
    return out


# ------------------------------------------------------- metric selection


def discriminability(scored: Sequence[Scored],
                     lapse_ms: float) -> Dict[str, Optional[float]]:
    """d = (fresh − tired) / within-state spread, per candidate.

    Fresh and tired are the outer thirds of `minutes_worked_before`. The
    cancellation test showed how this fails: if early sessions were all taken at
    zero worked time and later ones at high worked time, the split measures
    practice rather than fatigue. So the two groups are also required not to be
    separated in session index, and the check is reported rather than hidden.
    """
    usable = [s for s in scored if s.minutes_worked_before >= 0]
    if len(usable) < 8:
        return {name: None for name in CANDIDATES}

    worked = sorted(s.minutes_worked_before for s in usable)
    low, high = worked[len(worked) // 3], worked[(2 * len(worked)) // 3]
    if low == high:
        return {name: None for name in CANDIDATES}

    fresh = [s for s in usable if s.minutes_worked_before <= low]
    tired = [s for s in usable if s.minutes_worked_before >= high]
    if len(fresh) < 3 or len(tired) < 3:
        return {name: None for name in CANDIDATES}

    out: Dict[str, Optional[float]] = {}
    for name in CANDIDATES:
        a = [s.metrics(lapse_ms)[name] for s in fresh]
        b = [s.metrics(lapse_ms)[name] for s in tired]
        a = [v for v in a if v is not None]
        b = [v for v in b if v is not None]
        if len(a) < 2 or len(b) < 2:
            out[name] = None
            continue
        pooled = math.sqrt((pstdev(a) ** 2 + pstdev(b) ** 2) / 2)
        out[name] = ((mean(a) - mean(b)) / pooled * DIRECTION[name]
                     if pooled else None)
    return out


def confound_warning(scored: Sequence[Scored]) -> Optional[str]:
    """Is the fatigue axis just the session index in disguise?"""
    usable = [s for s in scored if s.minutes_worked_before >= 0]
    if len(usable) < 8:
        return None
    worked = sorted(s.minutes_worked_before for s in usable)
    low, high = worked[len(worked) // 3], worked[(2 * len(worked)) // 3]
    fresh = [s for s in usable if s.minutes_worked_before <= low]
    tired = [s for s in usable if s.minutes_worked_before >= high]
    if len(fresh) < 3 or len(tired) < 3:
        return None
    gap = mean(s.session_index for s in tired) - mean(s.session_index for s in fresh)
    span = max(s.session_index for s in usable) - min(s.session_index for s in usable)
    if span and abs(gap) / span > 0.25:
        return (f"наработка и номер сессии смешаны: «уставшие» в среднем на "
                f"{gap:+.1f} сессии позже «свежих» при размахе {span}. "
                f"d ниже может отражать научение, а не утомление.")
    return None


def main_metric(scored: Sequence[Scored],
                lapse_ms: float) -> Tuple[str, Optional[str]]:
    """The winner, or a reason there isn't one."""
    scores = discriminability(scored, lapse_ms)
    ranked = sorted(((v, k) for k, v in scores.items() if v is not None),
                    reverse=True)
    if not ranked:
        return "inv_rt", "данных мало"
    best, name = ranked[0]
    if best < MIN_D:
        return "inv_rt", f"ни один кандидат не дотянул: лучший d = {best:+.2f}"
    if len(ranked) > 1 and best - ranked[1][0] < MIN_MARGIN:
        return "inv_rt", (f"{name} и {ranked[1][1]} неразличимы "
                          f"({best:+.2f} против {ranked[1][0]:+.2f})")
    return name, None


# ---------------------------------------------------------------- verdict


def _pct(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    if value is None or reference in (None, 0):
        return None
    return (value / reference - 1.0) * 100.0


def after_run(session_id: str) -> Optional[str]:
    scored = load_scored()
    by_id = {s.session_id: s for s in scored}
    current = by_id.get(session_id)
    if current is None:
        return None

    lapse_ms, origin = lapse_threshold(scored)
    m = current.metrics(lapse_ms)

    lines = [
        f"проб {len(current.rts)}   медиана {m['median_rt']:.0f} мс   "
        f"1/RT {m['inv_rt']:.2f} с⁻¹",
        f"лапсов {int(m['lapses'])} (порог {lapse_ms:.0f} мс, {origin})   "
        f"фальстартов {current.n_false_starts}",
    ]

    anchor = by_id.get(current.anchor_id)
    if current.is_anchor or anchor is None or anchor.session_id == session_id:
        lines.append("Это якорь — с ним сравнятся следующие прогоны.")
    else:
        am = anchor.metrics(lapse_ms)
        parts = []
        for name, label in (("inv_rt_adj", "скорость"),
                            ("slowest10", "медленные 10%"),
                            ("fastest10_rt", "быстрые 10%")):
            d = _pct(m[name], am[name])
            if d is None:
                continue
            if DIRECTION[name] < 0:
                d = -d
            parts.append(f"{label} {d:+.0f}%")
        lines.append("к якорю:    " + "   ".join(parts))
        lines.append(f"            лапсов у якоря {int(am['lapses'])}, "
                     f"сейчас {int(m['lapses'])}")

    earlier = [s for s in scored if s.session_index < current.session_index]
    if earlier:
        pm = earlier[-1].metrics(lapse_ms)
        d = _pct(m["inv_rt_adj"], pm["inv_rt_adj"])
        if d is not None:
            lines.append(f"к прошлому: скорость {d:+.0f}%   "
                         f"лапсов было {int(pm['lapses'])}")

    quality = current.timing.get("presentation_reported", 1.0)
    if quality < 0.9:
        lines.append("")
        lines.append(f"ВНИМАНИЕ: реальное время показа известно только для "
                     f"{quality*100:.0f}% кадров — хронометраж этого прогона хуже "
                     f"обычного.")

    return "\n".join(lines)


# ----------------------------------------------------------------- report


def report(with_legend: bool = True) -> None:
    every = [score(r) for r in store.load_all()]
    every = [s for s in every if s is not None and s.rts]
    scored = [s for s in every if s.protocol_version == store.PROTOCOL_VERSION]

    if not every:
        print("Записей нет.")
        return

    older = len(every) - len(scored)
    usable_modality = [s for s in scored if s.modality != "mixed"]
    modality = usable_modality[-1].modality if usable_modality else "key"
    other_device = [s for s in scored if s.modality != modality]
    mixed = [s for s in scored if s.modality == "mixed"]
    scored = [s for s in scored if s.modality == modality]

    print(f"Сессий: {len(scored)} (протокол v{store.PROTOCOL_VERSION}, "
          f"ответ {modality})"
          + (f"   отложено по версии протокола: {older}" if older else "")
          + (f"   по другому устройству ответа: {len(other_device)}"
             if other_device else ""))
    if mixed:
        print(f"ВНИМАНИЕ: в {len(mixed)} сессиях смешаны клавиша и мышь — "
              f"они отложены целиком.")
    lost = sum(s.n_focus_lost for s in scored)
    if lost:
        print(f"Проб выброшено из-за потери фокуса окна: {lost}")
    off = sum(s.n_off_device for s in scored)
    if off:
        print(f"Проб выброшено как ответы не тем устройством: {off}")
    if not scored:
        print("Сопоставимых данных ещё нет.")
        return

    lapse_ms, origin = lapse_threshold(scored)
    print(f"Порог лапса: {lapse_ms:.0f} мс ({origin}; "
          f"стандарт {store.LAPSE_MS_NOMINAL})")

    columns = [("#", 3), ("время", 11), ("триггер", 9), ("якорь", 5),
               ("проб", 5), ("медRT", 6), ("1/RT", 5), ("испр", 5),
               ("медл10", 6), ("быстр10", 7), ("лапс", 4), ("фальст", 6),
               ("KSS", 4)]

    def row(values):
        return " ".join(str(v).rjust(w) for (_, w), v in zip(columns, values))

    print()
    print(row([name for name, _ in columns]))
    for s in scored:
        m = s.metrics(lapse_ms)
        print(row([
            s.session_index, s.started_at[5:16].replace("T", " "), s.trigger,
            "да" if s.is_anchor else "", len(s.rts),
            f"{m['median_rt']:.0f}", f"{m['inv_rt']:.2f}", f"{m['inv_rt_adj']:.2f}",
            f"{m['slowest10']:.2f}", f"{m['fastest10_rt']:.0f}",
            int(m["lapses"]), s.n_false_starts,
            "" if s.kss is None else s.kss,
        ]))

    print()
    print("Поправка на преднастройку (насколько интервал перед стимулом двигает"
          " ответ):")
    for s in scored:
        gain = (1 - s.fp_resid_sd / pstdev([1000 / x for x in s.rts])) * 100 \
            if len(s.rts) > 1 else 0.0
        print(f"   #{s.session_index}: наклон {s.fp_slope:+.3f} на log(мс), "
              f"дисперсия срезана на {gain:.0f}%")

    rated = [s for s in scored if s.kss is not None]
    agreement = kss_agreement(scored, lapse_ms)
    print()
    if len(rated) < 4:
        print(f"Самооценка (KSS): сессий с оценкой {len(rated)}, "
              f"для связи нужно хотя бы 4.")
    else:
        print(f"Согласие с самооценкой по {len(rated)} сессиям "
              f"(ранговая корреляция; выше нуля — метрика движется вместе с "
              f"сонливостью):")
        for name in CANDIDATES:
            v = agreement[name]
            print(f"   {name:<13} rho = "
                  + (f"{v:+.3f}" if v is not None else "  --"))

    warning = confound_warning(scored)
    metric, why = main_metric(scored, lapse_ms)
    scores = discriminability(scored, lapse_ms)
    print()
    if warning:
        print("ВНИМАНИЕ: " + warning)
    if why:
        print(f"Главная метрика не выбрана — {why}. Считаю по inv_rt.")
    else:
        print(f"Главная метрика: {metric}")
    if any(v is not None for v in scores.values()):
        for name in CANDIDATES:
            v = scores[name]
            print(f"   {name:<13} d = "
                  + (f"{v:+.3f}" if v is not None else "  --"))

    pairs = pairs_for(scored, lapse_ms)
    ratio_metric = metric if metric in RATIOABLE else "inv_rt_adj"
    usable = [p for p in pairs if p.ratios.get(ratio_metric) is not None]
    print()
    print(f"Пар «якорь — замер» по {ratio_metric}: {len(usable)} "
          f"(порог по данным с {MIN_PAIRS})")
    history: List[float] = []
    for pair in usable:
        r = pair.ratios[ratio_metric]
        cuts = thresholds(history)
        label = {"ok": "норма", "moderate": "умеренное",
                 "strong": "сильное"}[classify(r, cuts)]
        basis = "заглушка" if cuts["source"] == 0 else f"по {int(cuts['source'])} парам"
        print(f"   #{pair.session.session_index:<3d} r = {r:.3f}   "
              f"порог {cuts['moderate']:.3f} ({basis})   {label}")
        history.append(r)

    if with_legend:
        print()
        print(LEGEND)


LEGEND = """
ЧТО ЗДЕСЬ СЧИТАЕТСЯ

Проба даёт одно число — время реакции. Ответ быстрее 100 мс и нажатие до
стимула — это фальстарты, они не входят в расчёт времени, но считаются
отдельно. Проба без ответа за 5 секунд бросается.

Метрики
  медRT     медиана времени реакции
  1/RT      среднее обратного времени. Основной показатель в литературе:
            медленная проба даёт малый вклад, поэтому лапсы попадают в него
            естественным образом, без отдельного порога.
  испр      то же, приведённое к интервалу 2500 мс. Время реакции сильно
            зависит от того, сколько ждал стимула: чем дольше не появлялся,
            тем выше вероятность, что вот-вот появится, и тем лучше готов.
            Разные прогоны вытягивают разные наборы интервалов, поэтому
            сравнивать надо в одной точке этой кривой.
  медл10    среднее 1/RT по самой медленной десятой части проб. Там живут
            лапсы, и по литературе это самый чувствительный к утомлению
            показатель.
  быстр10   среднее RT по самой быстрой десятой. Показывает потолок
            возможностей, почти не зависящий от того, насколько собран.
  лапс      число проб медленнее порога
  фальст    нажатия до стимула плюс ответы быстрее 100 мс

Порог лапса
  Стандартные 355 мс подобраны на людях с базой около 280 мс. Он берётся
  только пока своих данных мало. Дальше порог считается по якорным прогонам
  как квантиль, оставляющий примерно 10% лапсов в отдохнувшем состоянии.
  Причина в арифметике: при сотне проб счёт около 11 имеет стандартную ошибку
  примерно 3.3, то есть 30% — с этим можно работать. Счёт, равный единице,
  не несёт информации вообще.

Парная схема
  Якорь — первый прогон после перерыва в 6 часов и больше. r = замер / якорь,
  ниже единицы — хуже. До 10 пар порог заглушечный: 15% и 25%. Дальше — по
  фактическому разбросу r в окне из 30 пар, μ − 1σ и μ − 2σ.

  Счётные метрики — лапсы и фальстарты — в парную схему НЕ входят. Отношение
  двух малых счётчиков скачет в десятки раз; в предыдущем инструменте это уже
  давало пороги, которые не могли сработать никогда. В соревнование метрик они
  входят, в отношение — нет.

Выбор главной метрики
  d = (среднее у свежего − среднее у уставшего) / разброс внутри состояния.
  Границы «свежий» и «уставший» — внешние трети распределения наработки.
  Кандидат должен набрать |d| не меньше 0.5 и обойти второго не меньше чем на
  0.2, иначе результат — «не выбрана». Иначе побеждает шум.

  Отдельно проверяется, не совпадает ли ось наработки с номером сессии: если
  «уставшие» замеры систематически позже «свежих», d измеряет научение, а не
  утомление, и об этом печатается предупреждение.

Самооценка
  После прогона и ДО показа цифр спрашивается шкала KSS: 1 крайне бодр … 9
  борюсь со сном. Порядок не случаен. Оценка после сводки была бы отчасти
  пересказом сводки, и связь между ними оказалась бы связью измерения с самим
  собой. Оценка до прогона задавала бы ему настрой. Между ними — единственное
  чистое место.

  Оценка НЕ считается истиной: инструмент затем и существует, что своему
  ощущению доверять нельзя. Она даёт вторую ось, независимую от измерения.
  Совпали — уверенность растёт. Разошлись — вот это и есть находка, ровно то,
  ради чего §1 задумывал запуск «по собственному ощущению спада».

  Связь считается ранговой корреляцией: шаги KSS не равны между собой, и
  обычная корреляция приписала бы шкале метрику, которой у неё нет. Знак
  скорректирован так, что плюс всегда означает «движется вместе с сонливостью».

Устройство ответа
  Клавиша и мышь не смешиваются. Литература ожидает, что мышь на 20–30 мс
  медленнее; на этой машине измерено обратное и гораздо больше — медиана 186 мс
  мышью против 251 мс пробелом, разрыв 65 мс. Причина скорее всего в частоте
  опроса устройства и в позе руки. Направление неважно: разрыв в несколько раз
  больше любого искомого эффекта утомления, поэтому пулы раздельные.

  Пул задаётся устройством последней однородной сессии. Если в сессии одно
  устройство занимает не меньше 95% проб, она считается однородной, а редкие
  чужие пробы выбрасываются: три нажатия клавиши среди сотни кликов не повод
  терять сотню. Ниже этой доли — это действительно две популяции, и сессия
  откладывается целиком.

Потеря фокуса
  Hyprland не отдаёт приложению тот клик, которым окно возвращает себе фокус.
  Ответ проглатывается, а следующий несёт в себе весь этот крюк — так в первом
  прогоне появилась «реакция» в 1574 мс. Проба, в течение которой окно теряло
  фокус, помечается и в расчёт не идёт.

Версии протокола
  Пулятся только записи одной версии. Смена длительности или интервала делает
  прогоны измерениями разных величин, и складывать их нельзя.
""".strip("\n")


# --------------------------------------------------------------- selftest


SYNTHETIC = {
    "session_id": "selftest",
    "session_index": 0,
    "started_at": "2026-01-01T00:00:00+00:00",
    "context": {"trigger": "manual", "mode": "training", "is_anchor": True,
                "anchor_session_id": "selftest", "minutes_worked_before": 0,
                "block_index": 0, "local_hour": 12, "hours_since_previous": None},
    "protocol": {"name": "PVT-B", "version": store.PROTOCOL_VERSION,
                 "valid_rt_min_ms": 100},
    "timing": {},
    "trials": [
        {"i": 0, "isi_ms": 2500, "onset_ms": 2500, "rt_ms": 200, "false_starts": []},
        {"i": 1, "isi_ms": 2500, "onset_ms": 5000, "rt_ms": 300, "false_starts": []},
        {"i": 2, "isi_ms": 2500, "onset_ms": 7500, "rt_ms": 400, "false_starts": []},
        {"i": 3, "isi_ms": 2500, "onset_ms": 10000, "rt_ms": 500, "false_starts": []},
        # A press before the stimulus, and a response too fast to be a reaction:
        # both are errors of commission, neither is a reaction time.
        {"i": 4, "isi_ms": 2500, "onset_ms": 12500, "rt_ms": 80,
         "false_starts": [-400.0]},
        # Abandoned at the cap.
        {"i": 5, "isi_ms": 2500, "onset_ms": 15000, "rt_ms": None,
         "false_starts": []},
    ],
}


def selftest() -> int:
    s = score(SYNTHETIC)
    assert s is not None
    failures = []

    def check(label, got, want, tol=1e-9):
        ok = abs(got - want) < tol
        if not ok:
            failures.append(f"{label}: ждали {want}, получили {got}")
        print(f"  {label:<22} {got!s:>10}   ждали {want!s:>10}   "
              f"{'ok' if ok else 'ПРОВАЛ'}")

    check("валидных проб", len(s.rts), 4)
    check("фальстартов", s.n_false_starts, 2)
    check("без ответа", s.n_no_response, 1)

    m = s.metrics(350.0)
    inv = mean([1000 / x for x in (200, 300, 400, 500)])
    check("1/RT", round(m["inv_rt"], 9), round(inv, 9))
    check("медиана RT", m["median_rt"], 350.0)
    # A tenth of four trials rounds to one, so the slowest and fastest tenths
    # are single trials -- checked here because that rounding is where an
    # off-by-one would hide.
    check("медленные 10%", round(m["slowest10"], 9), round(1000 / 500, 9))
    check("быстрые 10%", m["fastest10_rt"], 200.0)
    check("лапсов при 350", m["lapses"], 2.0)
    check("лапсов при 450", s.metrics(450.0)["lapses"], 1.0)
    check("доля лапсов при 350", round(s.lapse_rate(350.0), 9), 0.5)

    # Every ISI is identical here, so the foreperiod fit has nothing to lean on
    # and must fall back to the plain mean rather than dividing by zero.
    check("испр. при одном ISI", round(m["inv_rt_adj"], 6), round(inv, 6))

    print()
    if failures:
        for line in failures:
            print("  " + line)
        print(f"ПРОВАЛЕНО: {len(failures)}")
        return 1
    print("Счётчики и метрики сходятся с ручным расчётом.")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "report":
        report(with_legend=(len(sys.argv) < 3 or sys.argv[2] != "short"))
        return 0
    if command == "legend":
        print(LEGEND)
        return 0
    if command == "selftest":
        return selftest()
    if command == "after":
        if len(sys.argv) < 3:
            print("after <session_id>", file=sys.stderr)
            return 1
        line = after_run(sys.argv[2])
        print(line if line else "(нет данных)")
        return 0
    print("report [short] | legend | selftest | after <id>", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
