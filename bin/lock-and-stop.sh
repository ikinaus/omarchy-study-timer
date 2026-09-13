#!/usr/bin/env bash
# Stops the study timer, then hands off to the Omarchy lock command.
# Bind this instead of `omarchy-system-lock` directly.
#
# The path is derived, not hardcoded. The previous version still pointed at
# ~/.config/waybar/scripts/timer.sh -- dead since the Omarchy 4.0 migration --
# and the `|| true` swallowed the failure, so locking silently stopped stopping
# the clock. Failure is still tolerated, but it is now reported.

TIMER="$(cd "$(dirname "$0")/.." && pwd)/timer.sh"

if [ -r "$TIMER" ]; then
  bash "$TIMER" stop || echo "lock-and-stop: '$TIMER stop' failed" >&2
else
  echo "lock-and-stop: timer not found at $TIMER" >&2
fi

LOCK_BIN="$(command -v omarchy-system-lock || true)"
[ -z "$LOCK_BIN" ] && LOCK_BIN="$HOME/.local/share/omarchy/bin/omarchy-system-lock"

exec "$LOCK_BIN" "$@"
