#!/usr/bin/env bash
# Fires the timer's notifications on demand, so they can be looked at without
# waiting for the conditions that normally produce them.
#
#   ./notify-probe.sh nag | hour | norm | rollover | question | all
#
# Works on a COPY of the tree in a temp directory, with its own data/. The real
# state file, history and lock are never touched, so this can run while the
# service is ticking: `timer.sh` derives every path from its own location, which
# is the property that makes the copy independent.
#
# Two things are deliberately NOT faked, because they are what is being tested:
# the notification daemon and the presence probes. A snoozed timer will produce
# nothing here either -- the snooze file lives in $XDG_RUNTIME_DIR and is shared
# with the real one.
set -u

SRC=${STUDY_DIR:-$HOME/.config/omarchy/study}
SCENARIO=${1:-all}

[ -r "$SRC/timer.sh" ] || { echo "no timer.sh under $SRC" >&2; exit 1; }

T=$(mktemp -d /tmp/study-probe.XXXXXX)
trap 'rm -rf "$T"' EXIT
cp "$SRC/timer.sh" "$T/"
cp -r "$SRC/assets" "$SRC/bin" "$T/" 2>/dev/null
mkdir -p "$T/data"

NOW=$(date +%s)
TODAY=$(date -d "@$((NOW - 8 * 3600))" +%Y-%m-%d)

# Every key the script knows, at rest. Each scenario overrides a few.
write_state() {
  {
    echo "total=0";            echo "since=$NOW"
    echo "running=0";          echo "notified_hours=0"
    echo "day=$TODAY";         echo "mode=0"
    echo "nag_level=0";        echo "nag_at=0"
    echo "absent_since=0";     echo "norm_notified=0"
    echo "idle_since=0";       echo "pvt_next_min=90"
    echo "pvt_postpone_level=0"; echo "idle_before_start=0"
    echo "end_asks=0";         echo "end_answered=0"
    echo "end_next_idle=0";    echo "snooze_was=0"
    echo "idle_present=0";     echo "away_since=0"
    echo "away_gap=0";         echo "back_at=0"
    echo "anchor_used_at=0";   echo "last_tick=$NOW"
    for kv in "$@"; do echo "$kv"; done
  } > "$T/data/study_timer_state"
}

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

probe_nag() {
  say "nag — 1 h 36 min idle, 2:07 worked"
  echo "  expected:  Idle 1:36. 7:53 still to go today.   + slug.mp3"
  write_state "total=$((2 * 3600 + 7 * 60))" \
              "idle_present=$((96 * 60))" "nag_at=$((NOW - 60))"
  bash "$T/timer.sh" tick
}

probe_hour() {
  say "hour — the third whole hour, clock running"
  echo "  expected:  3 hour(s) done.   + welldone.mp3"
  write_state "running=1" "total=$((3 * 3600))" "since=$NOW" "notified_hours=2"
  bash "$T/timer.sh" tick
}

probe_norm() {
  say "norm — the daily target met"
  echo "  expected:  Daily norm met — 10 h.   + welldone.mp3"
  write_state "running=1" "total=$((10 * 3600))" "since=$NOW" "norm_notified=0"
  bash "$T/timer.sh" tick
}

probe_rollover() {
  say "rollover — 08:00, day closed, ceremony not yet handed over"
  echo "  expected:  End of day  7:05 today. …   + goodboy.mp3"
  write_state "day=1999-01-01" "total=$((7 * 3600 + 5 * 60))" "end_answered=0"
  bash "$T/timer.sh" tick
}

probe_question() {
  say "question — past half the norm, 45 min of idle at the machine"
  echo "  expected:  a two-button window. Pressing 'Да' plays the ceremony,"
  echo "             pressing 'Ещё поработаю' leaves it silent."
  write_state "total=$((6 * 3600 + 30 * 60))" "idle_present=$((46 * 60))"
  bash "$T/timer.sh" tick
  # The helper is detached, so without waiting this script would reach its trap
  # and delete the directory the helper is still reading from.
  #
  # Waiting on the helper's own lock rather than on a process name: `pgrep -f
  # session-end` also matches any shell whose command line happens to contain
  # the string, this script's own invocation included, and that turns the wait
  # into a hang. The lock is held for exactly as long as a window is up.
  #
  # It is the same lock the real timer's question uses, so if one of those is
  # already on screen this scenario produces nothing and returns at once. That
  # is the correct behaviour: two of these windows must never be open together.
  echo "  (waiting for the window; it times out on its own after 2 min)"
  sleep 2
  local waited=0
  local lock="${XDG_RUNTIME_DIR:-/tmp}/omarchy-study-end.lock"
  while [ "$waited" -lt 150 ]; do
    flock -n "$lock" true 2> /dev/null && break
    sleep 2
    waited=$((waited + 2))
  done
  grep -E '^(end_asks|end_answered)=' "$T/data/study_timer_state" | sed 's/^/  /'
}

case "$SCENARIO" in
  nag)      probe_nag ;;
  hour)     probe_hour ;;
  norm)     probe_norm ;;
  rollover) probe_rollover ;;
  question) probe_question ;;
  all)
    for p in nag hour norm rollover; do
      "probe_$p"
      sleep 4
    done
    probe_question
    ;;
  *)
    echo "usage: $0 nag | hour | norm | rollover | question | all" >&2
    exit 1
    ;;
esac

say "done — the real state file was not touched"
