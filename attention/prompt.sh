#!/usr/bin/env bash
# Offers the attention test, runs it if accepted, and reports the outcome back
# to the timer.
#
# This exists as a separate, detached process for one reason. `timer.sh tick`
# holds an flock on the state file for as long as it runs, and the offer can sit
# on screen for two minutes before the test itself takes another two. Doing that
# inside the tick would hold the lock for four minutes: every following tick and
# every click on the bar widget would queue behind it, and the timer would
# appear frozen. So the tick only detects that a threshold was crossed and
# launches this; the lock is taken again, briefly, by the `attention-result`
# call at the end.
#
#   prompt.sh <worked_minutes> <postpone_minutes>
set -u

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMER="$ROOT_DIR/timer.sh"
ATTENTION="$ROOT_DIR/attention/attention.py"

# Hardcoded on purpose. `python3` on this machine resolves to a virtualenv, and
# python-gobject is an Arch system package that no venv can see.
PYTHON=/usr/bin/python3

WORKED=${1:-0}
POSTPONE=${2:-15}

# tmpfs, so it cannot outlive the session and needs no cleanup path. Guards
# against a second offer opening while one is already on screen -- the tick that
# launched this runs again every minute.
LOCK="${XDG_RUNTIME_DIR:-/tmp}/omarchy-attention-prompt.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

choice=$("$PYTHON" "$ATTENTION" prompt --worked "$WORKED" --postpone-minutes "$POSTPONE" 2>/dev/null | tail -n 1)

case "$choice" in
  run)
    block=$((WORKED / 90))
    "$PYTHON" "$ATTENTION" run \
      --trigger block_end --block "$block" --worked "$WORKED" > /dev/null 2>&1
    # The outcome is reported as "run" whether or not the run was completed. A
    # test walked out of still means the offer was answered, and re-offering it
    # thirty seconds later is exactly the nagging §1 rules out.
    ;;
  postpone | skip) ;;
  *)
    # No answer at all -- the window failed to open, or the toolkit is missing.
    # Treated as a postpone so the threshold does not sit permanently overdue
    # and re-launch this every minute.
    choice=postpone
    ;;
esac

bash "$TIMER" attention-result "$choice" "$WORKED"
