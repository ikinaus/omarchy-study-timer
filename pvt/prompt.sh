#!/usr/bin/env bash
# Offers the vigilance test, runs it if accepted, reports the outcome back.
#
# Detached from `timer.sh tick` on purpose. The tick holds an flock on the state
# file for its whole run, and the offer can sit on screen for two minutes before
# the test takes another five. Seven minutes of held lock would queue every
# following tick and every click on the bar widget behind it, and the timer
# would look frozen. So the tick only detects the crossing and launches this;
# the lock is taken again, briefly, by the `pvt-result` call at the end.
#
#   prompt.sh <worked_minutes> <postpone_minutes> [trigger]
set -u

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMER="$ROOT_DIR/timer.sh"
PVT="$ROOT_DIR/pvt/pvt.py"

# Hardcoded. `python3` resolves to a virtualenv on this machine, and
# python-gobject is an Arch system package no venv can see.
PYTHON=/usr/bin/python3

WORKED=${1:-0}
POSTPONE=${2:-15}
TRIGGER=${3:-block_end}

# tmpfs, so it cannot outlive the session and needs no cleanup. Guards against a
# second offer opening while one is already up -- the tick that launched this
# runs again every minute.
LOCK="${XDG_RUNTIME_DIR:-/tmp}/omarchy-pvt-prompt.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

choice=$("$PYTHON" "$PVT" prompt --worked "$WORKED" \
  --postpone-minutes "$POSTPONE" 2>/dev/null | tail -n 1)

case "$choice" in
  run)
    block=$((WORKED / 90))
    "$PYTHON" "$PVT" run --trigger "$TRIGGER" --block "$block" \
      --worked "$WORKED" > /dev/null 2>&1
    # Reported as "run" whether or not the run was completed. A test walked out
    # of still means the offer was answered, and re-offering it a minute later
    # is exactly the nagging that gets a tool abandoned.
    ;;
  postpone | skip) ;;
  *)
    # No answer at all -- the window failed to open, or the toolkit is missing.
    # Treated as a postpone so the threshold does not sit permanently overdue and
    # relaunch this every minute.
    choice=postpone
    ;;
esac

bash "$TIMER" pvt-result "$choice" "$WORKED"
