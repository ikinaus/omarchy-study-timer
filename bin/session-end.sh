#!/usr/bin/env bash
# Puts the end-of-day question on screen and reports the answer back.
#
# Detached from `timer.sh tick` for the same reason pvt/prompt.sh is: the tick
# holds the flock on the state file for its whole run, and this window can sit
# unanswered for two minutes. Two minutes of held lock would queue every
# following tick and every click on the bar widget behind it. So the tick only
# decides that the question is due; the bookkeeping comes back through
# `end-result`.
#
#   session-end.sh <worked_minutes> <norm_minutes> <ask_number> <ask_limit>
set -u

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMER="$ROOT_DIR/timer.sh"
ASK="$ROOT_DIR/bin/session-end.py"

# Hardcoded. `python3` resolves to a virtualenv here, and python-gobject is an
# Arch system package no venv can see.
PYTHON=/usr/bin/python3

WORKED=${1:-0}
NORM=${2:-600}
ASKED=${3:-1}
LIMIT=${4:-3}

# tmpfs, so it cannot outlive the session and needs no cleanup. Guards against a
# second window opening on top of the first -- the tick that launched this runs
# again every minute.
LOCK="${XDG_RUNTIME_DIR:-/tmp}/omarchy-study-end.lock"

# Note which descriptor: `tick` launched this while holding ITS lock on fd 9,
# and a child inherits that lock with the descriptor. Pointing fd 9 at our own
# file closes the inherited one and releases the timer -- without this line the
# `end-result` call at the bottom would block on a lock this very process holds.
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

choice=$("$PYTHON" "$ASK" --worked "$WORKED" --norm "$NORM" \
  --asked "$ASKED" --total "$LIMIT" 2>/dev/null | tail -n 1)

case "$choice" in
  yes | no) ;;
  *)
    # No answer at all -- the window failed to open, or the toolkit is missing.
    # Read as "no": the ceremony must never be handed over by an accident.
    choice=no
    ;;
esac

bash "$TIMER" end-result "$choice"
