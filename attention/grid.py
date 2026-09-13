# -*- coding: utf-8 -*-
"""Table generation for the attention test (корректурная проба).

Everything here is deterministic given a seed. The session record stores the
seed rather than the matrix, so the analysis module must be able to rebuild a
table from that seed alone -- possibly years later, possibly from another
language. Python's `random` cannot promise that: its stream is an
implementation detail of the interpreter, not a documented format. So the
generator carries its own PRNG. PCG32 was picked because it is short enough to
re-implement anywhere in twenty lines.

The draw order below is part of the format. Changing it changes what a stored
seed means, which silently invalidates every table already recorded. If it ever
has to change, bump GENERATOR_VERSION and record it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

GENERATOR_VERSION = 1

# Eight letters, «А» among them. «А» is the exception marker -- it has to occur
# in the table as filler -- and can therefore never be a row's target: the rule
# "cross out the letter the row starts with, underline it when «А» is to its
# left" would refer to itself. That leaves seven candidate targets.
#
# The pairs are the point: И/Н, Ш/Щ, Э/З differ by one stroke each, which is
# what makes the task a test of attention rather than of reading speed. «Б» has
# no partner -- the eighth slot is spent on «А».
ALPHABET: Sequence[str] = ("А", "И", "Н", "Ш", "Щ", "Э", "З", "Б")
MARKER = "А"
TARGETS: Sequence[str] = tuple(ch for ch in ALPHABET if ch != MARKER)

ROWS = 80
COLS = 40

# Column 0 of every row is the instruction letter: shown, never scored, never
# crossable. So a full row contributes COLS - 1 = 39 scorable positions.
SCORABLE_PER_ROW = COLS - 1

# Per-row draws rather than fixed quotas. The user wrote the spec and knows the
# nominal frequencies, so a fixed rate would let him predict a row's load; a
# rate drawn per row does not. The ranges are deliberately narrow -- §2.4 of the
# spec works out that widening them to U(0.04, 0.20) doubles Var(K) and buys
# nothing.
P_TARGET_RANGE = (0.08, 0.16)
P_EXC_RANGE = (0.15, 0.35)


class PCG32:
    """Minimal PCG-XSH-RR 64/32. Reproducible across languages and versions."""

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
        """Uniform in [0, 1)."""
        return self.next_uint32() / 4294967296.0

    def uniform(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * self.random()

    def below(self, n: int) -> int:
        """Uniform integer in [0, n), by rejection -- a plain modulo is biased."""
        threshold = (1 << 32) % n
        while True:
            r = self.next_uint32()
            if r >= threshold:
                return r % n

    def choice(self, seq: Sequence):
        return seq[self.below(len(seq))]


@dataclass
class Table:
    seed: int
    letters: List[List[str]]
    row_targets: List[str]
    key: List[Dict[str, object]]
    # Reproducible from the seed, kept only for tests and diagnostics. Never
    # serialised: the record stores the ranges, not the realised draws.
    row_params: List[Dict[str, float]] = field(default_factory=list)

    def key_map(self) -> Dict[tuple, str]:
        """(row, col) -> "target" | "exception"."""
        return {(e["row"], e["col"]): e["type"] for e in self.key}


def generate(seed: int) -> Table:
    rng = PCG32(seed)

    letters: List[List[str]] = []
    row_targets: List[str] = []
    key: List[Dict[str, object]] = []
    row_params: List[Dict[str, float]] = []

    previous_target: Optional[str] = None

    for r in range(ROWS):
        # Never the same target two rows running. Repeating it would hand the
        # reader a row with no reconfiguration to do, and reconfiguration cost
        # is part of what the task measures (§8.3) rather than something to be
        # sampled at random.
        target = rng.choice(TARGETS)
        while target == previous_target:
            target = rng.choice(TARGETS)
        previous_target = target

        p_target = rng.uniform(*P_TARGET_RANGE)
        p_exc = rng.uniform(*P_EXC_RANGE)

        is_target = [False] * COLS
        for c in range(1, COLS):
            if rng.random() < p_target:
                is_target[c] = True

        is_exception = [False] * COLS
        for c in range(1, COLS):
            if not is_target[c]:
                continue
            if rng.random() >= p_exc:
                continue
            # An exception needs «А» in the cell to its left, and that cell has
            # to be free. At c == 1 the left neighbour is the instruction
            # letter; when the left neighbour is itself a target it is already
            # spoken for. Neither can be rewritten, so the draw is demoted to a
            # regular target.
            #
            # This makes the realised exception rate slightly lower than p_exc.
            # Harmless, and deliberately not corrected: the analysis module
            # counts n_exc from the key, never from the nominal rate, so the
            # distortion never reaches a metric.
            if c >= 2 and not is_target[c - 1]:
                is_exception[c] = True

        row: List[Optional[str]] = [None] * COLS
        row[0] = target

        for c in range(1, COLS):
            if is_target[c]:
                row[c] = target

        for c in range(1, COLS):
            if is_exception[c]:
                row[c - 1] = MARKER

        for c in range(1, COLS):
            if row[c] is not None:
                continue
            # Two constraints on filler. It must never equal the row's target,
            # or it would BE a target by the rule while the key says otherwise.
            # And it must not be «А» directly left of a regular target, which
            # would silently promote that target to an exception the key does
            # not record. Both are invariants the self-test checks.
            pool = [ch for ch in ALPHABET if ch != target]
            following_regular_target = (
                c + 1 < COLS and is_target[c + 1] and not is_exception[c + 1]
            )
            if following_regular_target:
                pool = [ch for ch in pool if ch != MARKER]
            row[c] = rng.choice(pool)

        for c in range(1, COLS):
            if is_target[c]:
                key.append(
                    {
                        "row": r,
                        "col": c,
                        "type": "exception" if is_exception[c] else "target",
                    }
                )

        letters.append([ch for ch in row])  # type: ignore[misc]
        row_targets.append(target)
        row_params.append({"target": target, "p_target": p_target, "p_exc": p_exc})

    return Table(
        seed=seed,
        letters=letters,
        row_targets=row_targets,
        key=key,
        row_params=row_params,
    )


def validate(table: Table) -> None:
    """Invariants that must hold for the key to mean what the metrics assume."""
    km = table.key_map()

    for r, row in enumerate(table.letters):
        target = table.row_targets[r]

        assert row[0] == target, (r, "instruction letter must be the target")
        assert target != MARKER, (r, "«А» must never be a target")

        for c in range(1, COLS):
            is_key = (r, c) in km
            looks_like_target = row[c] == target

            # Every cell bearing the target letter is in the key, and nothing
            # else is. Without this a "miss" could be a generator bug.
            assert is_key == looks_like_target, (r, c, row[c], target, is_key)

            if not is_key:
                continue

            left_is_marker = row[c - 1] == MARKER
            expected = "exception" if left_is_marker else "target"
            assert km[(r, c)] == expected, (r, c, km[(r, c)], expected)


def counts(table: Table) -> Dict[str, float]:
    n_reg = sum(1 for e in table.key if e["type"] == "target")
    n_exc = sum(1 for e in table.key if e["type"] == "exception")
    total = ROWS * SCORABLE_PER_ROW
    return {
        "n_reg": n_reg,
        "n_exc": n_exc,
        "n": n_reg + n_exc,
        "rate_target": (n_reg + n_exc) / total,
        "rate_exc_given_target": n_exc / (n_reg + n_exc) if (n_reg + n_exc) else 0.0,
    }


if __name__ == "__main__":
    import statistics
    import sys

    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200

    rates, exc_rates, per_row = [], [], []
    for seed in range(runs):
        t = generate(seed)
        validate(t)
        c = counts(t)
        rates.append(c["rate_target"])
        exc_rates.append(c["rate_exc_given_target"])
        for r in range(ROWS):
            per_row.append(sum(1 for e in t.key if e["row"] == r))

    print(f"tables            {runs}, all invariants hold")
    print(f"nominal p_target  mean of U(0.08, 0.16) = 0.1200")
    print(f"realised          {statistics.mean(rates):.4f}"
          f"  sd {statistics.stdev(rates):.4f}")
    print(f"nominal p_exc     mean of U(0.15, 0.35) = 0.2500")
    print(f"realised          {statistics.mean(exc_rates):.4f}"
          f"  sd {statistics.stdev(exc_rates):.4f}  (demotion pulls this down)")
    print(f"targets per row   mean {statistics.mean(per_row):.2f}"
          f"  var {statistics.variance(per_row):.2f}"
          f"   (spec §2.4 predicts ~5.0)")

    a = generate(12345)
    b = generate(12345)
    assert a.letters == b.letters and a.key == b.key
    assert generate(12346).letters != a.letters
    print("reproducibility   same seed -> same table, different seed -> different")

    sample = generate(1)
    print("\nfirst three rows (column 0 is the instruction letter):")
    for r in range(3):
        print("  " + " ".join(sample.letters[r][:28]) + " …"
              f"   target {sample.row_targets[r]}")
