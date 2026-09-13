#!/bin/bash
# Study timer.
#
#   tick          advance state, fire reminders. Called once a minute by the
#                 plugin's service, NOT by the bar widget -- reminders must keep
#                 working when the widget is off the bar.
#   status        one line of JSON for the widget. Read-only side effects aside
#                 from the midnight rollover.
#   toggle        start / stop
#   stop          idempotent stop, safe to call when already stopped
#   toggle-mode   switch the percentage base
#   snooze        silence reminders for a few hours, deliberately awkward
#   unsnooze      cancel a snooze, no ceremony
#   report        today, weekly average, streak, best day
#   pvt           the vigilance test: run it, or pass a subcommand through
#   pvt-result    internal; how a prompted offer was answered
#   end-result    internal; how the end-of-day question was answered
#   attention     the cancellation test, by hand only -- no longer on the timer
#
# Paths derive from this script's own directory, so the tree moves without edits.
set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Layout, settled 2026-08-31. Everything still derives from the script's own
# location, so the tree can be moved whole without editing a path -- the change
# is only that code, data and assets no longer share one flat directory.
DATA_DIR="$ROOT_DIR/data"
ASSETS_DIR="$ROOT_DIR/assets"
ATTENTION_DIR="$ROOT_DIR/attention"

mkdir -p "$DATA_DIR"

STATE_FILE="$DATA_DIR/study_timer_state"
LOCK_FILE="$DATA_DIR/.study_timer.lock"
HISTORY="$DATA_DIR/study_history.tsv"
LEGACY_LOG="$DATA_DIR/study_history.log"

DAILY_SOUND_FILE="$ASSETS_DIR/slug.mp3"
WELLDONE_SOUND_FILE="$ASSETS_DIR/welldone.mp3"
GOODBOY_SOUND="$ASSETS_DIR/goodboy.mp3"

# ---------------------------------------------------------------- settings
NORM_HOURS=10
NORM_SECONDS=$((NORM_HOURS * 3600))

# Minutes of idle before each successive reminder. The last value repeats.
NAG_LADDER=(40 30 15 7 5)

# Reminders turn critical from this rung on.
NAG_CRITICAL_FROM=2

# --------------------------------------------------------- end of day
# The day turns at 08:00, an hour he is normally asleep through, so the
# closing sound and notification -- the only ceremony this thing has -- were
# being delivered to an empty room. Past half the norm a long idle stretch is
# a plausible end of the working day, so the question is put out loud and
# "yes" fires the ceremony there and then.
#
# It closes nothing. The clock keeps running, work after the answer still
# accrues to the same day, and 08:00 still does the bookkeeping.
END_IDLE_MIN=45
END_ASK_LIMIT=3
# Below this the question would be absurd: an hour's work is not a day.
END_GATE_SECONDS=$((NORM_SECONDS / 2))

# How long the machine must look unattended before the clock is stopped. Two
# minutes rides out a DPMS blink and still lands before the 150 s screensaver.
AWAY_GRACE=120

SNOOZE_HOURS=2
# $XDG_RUNTIME_DIR is tmpfs and is wiped when the session ends, so a snooze can
# never outlive the session -- no cleanup code required.
SNOOZE_FILE="${XDG_RUNTIME_DIR:-/tmp}/omarchy-study-snooze"

# ------------------------------------------------------------- vigilance
# The trigger now offers the PVT, not the cancellation test. The cancellation
# test could not detect fatigue at any tolerable run length: it is self-paced,
# so the speed/accuracy trade-off is the subject's to choose, and he chose it
# differently from run to run. See study/pvt/ for the replacement.
#
# Worked minutes between offers. Wall-clock would count breaks as work, and the
# whole question is how long *working* can go on before the result falls off.
PVT_INTERVAL_MIN=90

# Minutes to wait after "postpone". Worked minutes again, so a postponed offer
# cannot surface during a break. The last value repeats.
PVT_POSTPONE=(15 10 5)

# Idle time before a start counts as the beginning of a fresh stretch, at which
# point the anchor measurement is offered straight away instead of waiting a
# full interval. Matches ANCHOR_GAP_HOURS in pvt/store.py -- but only as a
# nudge: whether a run really is an anchor is decided there, from the gap since
# the last PVT, not from the timer's idea of idleness.
ANCHOR_IDLE_SECONDS=21600

# How soon after coming back the anchor is still worth taking. The gap above
# says he was away long enough to be rested; this says he has not since spent
# half the morning at the machine before starting the clock. Both numbers are
# recorded on every run, so the line can be moved later from data rather than
# from taste.
ANCHOR_FRESH_SECONDS=5400

# Absences shorter than this are not breaks. Without the floor a fifteen-minute
# coffee run would overwrite the record of last night's sleep, which is the one
# thing the morning anchor reads.
ABSENCE_MIN_SECONDS=1800

# A tick is scheduled every 60 s. A hole much larger than that means the machine
# was suspended or powered off -- an absence the presence probes never saw,
# because a sleeping machine does not run them.
TICK_GAP_MAX=300

# Hardcoded, never a bare `python3`: that resolves to a virtualenv here, and
# python-gobject is an Arch system package no venv can see.
PYTHON_BIN=/usr/bin/python3
ATTENTION_PY="$ATTENTION_DIR/attention.py"
ATTENTION_ANALYZE="$ATTENTION_DIR/analyze.py"

PVT_DIR="$ROOT_DIR/pvt"
PVT_PY="$PVT_DIR/pvt.py"
PVT_ANALYZE="$PVT_DIR/analyze.py"
PVT_PROMPT="$PVT_DIR/prompt.sh"
END_PROMPT="$ROOT_DIR/bin/session-end.sh"

# The day turns over at this hour, not at midnight. Work regularly runs past
# midnight here, and a calendar boundary split one work session across two days,
# making both halves meaningless. A day is labelled by the date it began.
DAY_START_HOUR=8

NOW=$(date +%s)
TODAY=$(date -d "@$((NOW - DAY_START_HOUR * 3600))" +%Y-%m-%d)
DAY_STARTED_AT=$(date -d "$TODAY $DAY_START_HOUR:00:00" +%s)

# ------------------------------------------------------------------- state
# key=value rather than positional fields: the old format needed a fallback for
# every field added, and adding one meant touching the read.
total=0
since=$NOW
running=0
notified_hours=0
day=$TODAY
mode=0
nag_level=0
nag_at=0
absent_since=0
norm_notified=0
# When the current idle stretch began. Separate from `since` on purpose: the
# rollover resets `since` so that the first nag of a new day does not report the
# whole night as idle time, and that reset would otherwise erase exactly the gap
# the anchor offer needs. With the day turning at 08:00 the rollover always falls
# between an evening stop and a morning start, so without this key the anchor
# would never be offered again.
idle_since=0
# Worked minutes at which the next offer is due, and how far down the postpone
# ladder we are. They live here rather than in the test's own state file: this
# script is the thing that ticks, and two copies of the same fact would diverge
# the first time an offer went unanswered.
pvt_next_min=0
pvt_postpone_level=0
# Length of the idle stretch that preceded the current start, in seconds,
# captured before `since` is reset. Without it the gap is unrecoverable: `since`
# doubles as the origin of the running segment the moment the clock starts.
idle_before_start=0
# Presence, tracked independently of the clock. `absent_since` above cannot
# be reused: it is cleared whenever the clock is stopped, which is exactly
# the stretch that has to be measured here. `away_since` is when the current
# absence began (0 while he is at the machine), `away_gap` the length of the
# last absence worth the name, `back_at` when it ended, `anchor_used_at` the
# `back_at` an anchor has already been offered for, and `last_tick` the
# previous tick, which is how a suspended machine gets noticed at all.
# Seconds of the current break actually spent AT the machine. The wall clock
# from the last stop is the wrong number: it counts a night's sleep as idle,
# and nothing about sleep belongs in "how long have you been sitting here
# not working". Absence is measured separately, by `away_gap` below.
idle_present=0
away_since=0
away_gap=0
back_at=0
anchor_used_at=0
last_tick=0
# The end-of-day question: how many times it has been put today, whether it
# was ever answered "yes" (which is what keeps 08:00 from announcing the day
# a second time), and when the next ask falls due.
end_asks=0
end_answered=0
# Present-idle seconds at which the next question falls due -- NOT an epoch.
# Wall clock would fire the question the moment he walked back in after two
# hours away, which is the one moment it is certainly wrong.
end_next_idle=0
# Whether a snooze was running at the previous tick. Only an edge detector:
# the snooze itself lives in a tmpfs file, and what matters is the moment it
# stops, which nothing else in the script would otherwise notice.
snooze_was=0

load_state() {
  [ -s "$STATE_FILE" ] || return 0

  # A file from before the key=value change: seven space-separated fields.
  if ! grep -q '=' "$STATE_FILE"; then
    read -r total since running notified_hours day mode _coefficient < "$STATE_FILE"
    nag_level=0
    nag_at=0
    return 0
  fi

  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      total) total=$value ;;
      since) since=$value ;;
      running) running=$value ;;
      notified_hours) notified_hours=$value ;;
      day) day=$value ;;
      mode) mode=$value ;;
      nag_level) nag_level=$value ;;
      nag_at) nag_at=$value ;;
      absent_since) absent_since=$value ;;
      norm_notified) norm_notified=$value ;;
      idle_since) idle_since=$value ;;
      pvt_next_min) pvt_next_min=$value ;;
      pvt_postpone_level) pvt_postpone_level=$value ;;
      idle_before_start) idle_before_start=$value ;;
      end_asks) end_asks=$value ;;
      end_answered) end_answered=$value ;;
      end_next_idle) end_next_idle=$value ;;
      snooze_was) snooze_was=$value ;;
      idle_present) idle_present=$value ;;
      away_since) away_since=$value ;;
      away_gap) away_gap=$value ;;
      back_at) back_at=$value ;;
      anchor_used_at) anchor_used_at=$value ;;
      last_tick) last_tick=$value ;;
    esac
  done < "$STATE_FILE"
}

save_state() {
  # Written to a sibling and renamed. The PVT now reads this file to snapshot
  # the timer's context, and `cat >` truncates before it writes: a reader
  # landing in that window would see a half-empty file and record nonsense.
  # Rename is atomic within a filesystem, so a reader sees either the old state
  # or the new one.
  cat > "$STATE_FILE.tmp" <<EOF
total=$total
since=$since
running=$running
notified_hours=$notified_hours
day=$day
mode=$mode
nag_level=$nag_level
nag_at=$nag_at
absent_since=$absent_since
norm_notified=$norm_notified
idle_since=$idle_since
pvt_next_min=$pvt_next_min
pvt_postpone_level=$pvt_postpone_level
idle_before_start=$idle_before_start
end_asks=$end_asks
end_answered=$end_answered
end_next_idle=$end_next_idle
snooze_was=$snooze_was
idle_present=$idle_present
away_since=$away_since
away_gap=$away_gap
back_at=$back_at
anchor_used_at=$anchor_used_at
last_tick=$last_tick
EOF
  mv -f "$STATE_FILE.tmp" "$STATE_FILE"
}

# Seconds banked today, including the run in progress.
elapsed() {
  if [ "$running" -eq 1 ]; then
    echo $((total + NOW - since))
  else
    echo "$total"
  fi
}

# --------------------------------------------------------------- presence
# Two cheap signals, both verified present on this machine. A dark screen is the
# stronger one; logind's IdleHint depends on something telling it about input,
# so it is treated as advisory.
# A missing tool must not silence reminders: for a discipline aid, failing quiet
# is worse than the occasional reminder into an empty room. Each probe therefore
# only votes "away" when it is actually there and actually says so.
is_present() {
  if command -v hyprctl > /dev/null 2>&1; then
    hyprctl monitors -j 2>/dev/null | grep -q '"dpmsStatus": *true' || return 1
  fi

  if command -v loginctl > /dev/null 2>&1; then
    [ "$(loginctl show-session "${XDG_SESSION_ID:-self}" -p IdleHint --value 2>/dev/null)" = "yes" ] && return 1
  fi

  locked && return 1

  return 0
}

locked() {
  command -v omarchy-shell > /dev/null 2>&1 || return 1
  [ "$(omarchy-shell lock isLocked 2>/dev/null)" = "true" ]
}

# A STRICTER test than is_present, and deliberately so.
#
# Suppressing a reminder on a weak signal costs one skipped notification.
# Stopping the clock on a weak signal costs real work: logind's IdleHint means
# "no input", and an hour of watching a lecture produces no input at all --
# which for a study timer is the thing being measured, not its absence.
#
# So the clock only stops on signals where the machine itself concluded nobody
# is there: the session is locked, or the screen is actually dark. Neither
# happens during playback, because players hold an idle inhibitor.
machine_unattended() {
  locked && return 0

  if command -v hyprctl > /dev/null 2>&1; then
    hyprctl monitors -j 2>/dev/null | grep -q '"dpmsStatus": *true' || return 0
  fi

  return 1
}

snoozed_until() {
  [ -s "$SNOOZE_FILE" ] || { echo 0; return; }
  local until
  until=$(cat "$SNOOZE_FILE" 2>/dev/null || echo 0)
  case "$until" in
    ''|*[!0-9]*) echo 0 ;;
    *) [ "$until" -gt "$NOW" ] && echo "$until" || echo 0 ;;
  esac
}

# ---------------------------------------------------------------- history
migrate_legacy_log() {
  [ -s "$LEGACY_LOG" ] || return 0
  [ -s "$HISTORY" ] && return 0

  # Old lines look like "2026-08-15: 03:42". Convert once; the original file is
  # left alone as an archive.
  awk -F'[: ]+' 'NF>=3 { printf "%s\t%d\n", $1, ($2*3600)+($3*60) }' "$LEGACY_LOG" > "$HISTORY"
}

record_day() {
  local date=$1 seconds=$2
  [ "$seconds" -gt 0 ] || return 0
  printf '%s\t%d\n' "$date" "$seconds" >> "$HISTORY"
}

# --------------------------------------------------------------- rollover
rollover() {
  [ "$day" = "$TODAY" ] || return 1

  return 0
}

do_rollover() {
  if [ "$running" -eq 1 ]; then
    total=$((total + NOW - since))
  fi

  if [ "$total" -gt 0 ]; then
    # Skipped whole when the ceremony was already handed over during the night.
    # The point of asking was to deliver it while he was awake; delivering it
    # again at 08:00 would make the earlier one meaningless.
    if [ "$end_answered" -eq 0 ]; then
      local h=$((total / 3600)) m=$(((total % 3600) / 60))
      notify-send -u critical "End of day" \
        "$h h $m min today. The day is closed, but nothing stops you from continuing."
      # The notification is left unconditional -- it lands in the history and
      # can be read later. The sound is not: the day now turns at 08:00, an hour
      # he is far more likely to be asleep through than midnight.
      if [ -r "$GOODBOY_SOUND" ] && is_present; then
        paplay "$GOODBOY_SOUND" &
      fi
    fi
    record_day "$day" "$total"
  fi

  total=0
  notified_hours=0
  nag_level=0
  nag_at=0
  norm_notified=0
  pvt_next_min=0
  pvt_postpone_level=0
  end_asks=0
  end_answered=0
  end_next_idle=0
  # `away_since`, `away_gap`, `back_at`, `anchor_used_at` and `last_tick`
  # are deliberately NOT reset. The night's absence straddles 08:00, and
  # clearing it here would destroy the one measurement the morning anchor
  # reads -- the same trap `idle_since` was introduced to avoid.
  day=$TODAY

  # Unconditional, and that is the point. While the clock runs this restarts the
  # current segment, which the banking above has already accounted for. While it
  # is stopped `since` is the origin of the idle counter, and nothing else ever
  # moves it during a stopped stretch -- so without this line an evening stop
  # made the first reminder of the next day report the whole night as idle time
  # ("Idle for 1346 min"). Midnight is now the ceiling.
  since=$NOW
}

# ------------------------------------------------------- presence history
# Runs on every tick, whether or not the clock does.
#
# What the anchor needs to know is how long he was away from the MACHINE and how
# recently he came back -- "slept eight hours, sat down twenty minutes ago". The
# old test asked how long the CLOCK had been off, which counts an evening spent
# at the machine not working as rest, and that is how a run at 19:41 became a
# day's anchor.
presence_track() {
  # A suspended or powered-off machine does not tick, so the probes never see
  # that absence. A hole in the tick sequence is the only evidence it happened,
  # and overnight it is usually the ONLY evidence there is.
  local delta=0
  if [ "$last_tick" -ne 0 ]; then
    delta=$((NOW - last_tick))
    [ "$delta" -lt 0 ] && delta=0
    if [ "$delta" -gt "$TICK_GAP_MAX" ]; then
      [ "$away_since" -eq 0 ] && away_since=$last_tick
      # The hole itself is absence, never idle: nobody was sitting here.
      delta=0
    fi
  fi
  last_tick=$NOW

  if machine_unattended; then
    [ "$away_since" -eq 0 ] && away_since=$NOW
    return 0
  fi

  # Present and not working: this is the only thing that counts as idle. A tick
  # at a time, so the total survives a stretch broken by three absences without
  # any of them being added to it.
  [ "$running" -eq 0 ] && idle_present=$((idle_present + delta))

  [ "$away_since" -eq 0 ] && return 0

  local gap=$((NOW - away_since))
  away_since=0
  [ "$gap" -lt "$ABSENCE_MIN_SECONDS" ] && return 0

  away_gap=$gap
  back_at=$NOW
}

# ------------------------------------------------------------- auto stop
# Detection lags by up to a tick, so the moment presence was lost is recorded
# and the rollback banks time only up to that point. Without it, walking away
# would credit however long it took to notice.
auto_stop() {
  if [ "$running" -ne 1 ]; then
    absent_since=0
    return 0
  fi

  if ! machine_unattended; then
    absent_since=0
    return 0
  fi

  if [ "$absent_since" -eq 0 ]; then
    absent_since=$NOW
    return 0
  fi

  [ $((NOW - absent_since)) -lt "$AWAY_GRACE" ] && return 0

  local banked=$((absent_since - since))
  [ "$banked" -lt 0 ] && banked=0
  total=$((total + banked))

  running=0
  since=$NOW
  # The idle stretch began when presence was lost, not when it was noticed.
  idle_since=$absent_since
  # He left, so the new break opens with nothing on it: the minutes between
  # walking out and this rollback are absence, not idle.
  idle_present=0
  absent_since=0
  nag_level=0
  nag_at=0

  notify-send -u normal "Study timer" \
    "Stopped: the machine was left unattended. Time after that was not counted."
}

# ------------------------------------------------------------- vigilance
pvt_postpone_interval() {
  local index=$1
  local last=$((${#PVT_POSTPONE[@]} - 1))
  [ "$index" -gt "$last" ] && index=$last
  echo "${PVT_POSTPONE[$index]}"
}

launch_pvt_offer() {
  local worked=$1 trigger=$2

  # Same gates as a reminder. An offer into an empty room is worse than a
  # reminder into one: it would be answered by its own timeout, spending a rung
  # of the postpone ladder on nobody.
  is_present || return 1
  [ "$(snoozed_until)" -gt 0 ] && return 1

  # Pushed out BEFORE the helper is launched. If the helper never reports back --
  # killed, no display, toolkit gone -- the offer retries in a few minutes rather
  # than relaunching every single minute forever.
  pvt_next_min=$((worked + 5))

  setsid bash "$PVT_PROMPT" "$worked" \
    "$(pvt_postpone_interval "$pvt_postpone_level")" "$trigger" \
    > /dev/null 2>&1 < /dev/null &
  return 0
}

# Detects that something is due and hands off. It does NOT show the offer: this
# runs inside `tick`, which holds the flock for its whole duration, and an offer
# can sit unanswered for two minutes before a five-minute test. Seven minutes of
# held lock would queue every following tick and every click on the bar widget
# behind it. So the window is opened by a detached helper, and the bookkeeping
# comes back through `pvt-result`.
pvt_check() {
  [ -r "$PVT_PROMPT" ] || return 0
  [ "$running" -eq 1 ] || return 0

  local worked=$(($(elapsed) / 60))

  if [ "$pvt_next_min" -eq 0 ]; then
    # First threshold of this stretch. If the clock was started after a long
    # idle, the anchor is offered NOW rather than a full interval into the work:
    # an anchor taken at the end of a block is not a rested baseline, and every
    # later measurement is divided by it.
    # Three conditions, not one. He was away long enough to have slept; he
    # came back recently enough that the clock's first minutes really are the
    # session's first minutes; and no anchor has been offered for this return
    # yet. `away_gap` is left standing afterwards -- the test reads it for the
    # record, and `anchor_used_at` is what stops a second offer.
    if [ "$away_gap" -ge "$ANCHOR_IDLE_SECONDS" ] &&
       [ "$back_at" -ne 0 ] && [ "$back_at" -ne "$anchor_used_at" ] &&
       [ $((NOW - back_at)) -le "$ANCHOR_FRESH_SECONDS" ]; then
      if launch_pvt_offer "$worked" session_start; then
        anchor_used_at=$back_at
        idle_before_start=0
      fi
      return 0
    fi
    pvt_next_min=$PVT_INTERVAL_MIN
    return 0
  fi

  [ "$worked" -lt "$pvt_next_min" ] && return 0
  launch_pvt_offer "$worked" block_end
}

# ----------------------------------------------------------- snooze edge
# Snoozed minutes still count as idle: he is at the machine and not working,
# which is the whole definition, and the vigilance record wants the truth about
# how long he sat there. What must not happen is that everything reading that
# idle goes off the instant the silence ends -- two hours of snooze would put
# the counter well past every threshold, so the reward for asking not to be
# disturbed would be a notification at the second it expired.
#
# `unsnooze` already resets the ladder for exactly this reason. A snooze that
# runs out on its own has nobody to do it, which is what this is for.
snooze_edge() {
  local active=0
  [ "$(snoozed_until)" -gt 0 ] && active=1

  if [ "$snooze_was" -eq 1 ] && [ "$active" -eq 0 ]; then
    nag_level=0
    nag_at=0
    end_next_idle=$((idle_present + END_IDLE_MIN * 60))
  fi

  snooze_was=$active
}

# --------------------------------------------------------- end of day
# Detects that the question is due and hands off, exactly like pvt_check: the
# window is opened by a detached helper and the answer comes back through
# `end-result`, so the flock is never held while a dialog waits.
#
# Both describe this tick only and are never persisted. `end_armed` says the
# question is live for this break, which is what silences the ordinary nag;
# `end_asked_now` says it actually went up, which silences the nag on the tick
# of the third and last question, after which `end_armed` stops being set.
end_armed=0
end_asked_now=0

end_of_day_check() {
  [ -r "$END_PROMPT" ] || return 0

  # Working is its own answer. The idle clock starts over at the next stop.
  if [ "$running" -eq 1 ]; then
    end_next_idle=0
    return 0
  fi

  [ "$end_answered" -eq 1 ] && return 0
  [ "$end_asks" -ge "$END_ASK_LIMIT" ] && return 0
  [ "$(elapsed)" -ge "$END_GATE_SECONDS" ] || return 0
  [ "$(snoozed_until)" -gt 0 ] && return 0
  is_present || return 0

  # Past every gate: from here on the question owns the interruption channel,
  # whether or not it is due this minute.
  end_armed=1

  # Measured in present idle, not wall clock. Forty-five minutes of sitting here
  # not working is a plausible end of the day; forty-five minutes of being out
  # of the house is not, and on the wall clock the two are the same number.
  [ "$end_next_idle" -eq 0 ] && end_next_idle=$((END_IDLE_MIN * 60))
  [ "$idle_present" -lt "$end_next_idle" ] && return 0

  # Counted and rescheduled BEFORE the helper is launched, for the same reason
  # the PVT threshold is: a helper that never reports back must not put the
  # question up again on the very next tick.
  end_asks=$((end_asks + 1))
  end_next_idle=$((idle_present + END_IDLE_MIN * 60))
  end_asked_now=1

  setsid bash "$END_PROMPT" "$(($(elapsed) / 60))" "$((NORM_SECONDS / 60))" \
    "$end_asks" "$END_ASK_LIMIT" > /dev/null 2>&1 < /dev/null &
  return 0
}

# ------------------------------------------------------------- reminders
nag_interval() {
  local index=$1
  local last=$((${#NAG_LADDER[@]} - 1))
  [ "$index" -gt "$last" ] && index=$last
  echo "${NAG_LADDER[$index]}"
}

reminders() {
  local current
  current=$(elapsed)

  # The one goal this thing has. Announced once a day, ahead of the
  # running/stopped split: the norm is normally crossed with the clock
  # running, and that branch returns before the silence check further down is
  # ever reached, so a check placed there would never fire on a working day.
  # `notified_hours` is advanced here as well because the norm sits on a whole
  # hour, so without it the hourly milestone would fire in the same tick and
  # play the same sound on top of itself.
  if [ "$norm_notified" -eq 0 ] && [ "$current" -ge "$NORM_SECONDS" ]; then
    notify-send -u normal "Study timer" \
      "Daily norm met — $NORM_HOURS h. Reminders are off until tomorrow."
    [ -r "$WELLDONE_SOUND_FILE" ] && paplay "$WELLDONE_SOUND_FILE" &
    notified_hours=$((current / 3600))
    norm_notified=1
  fi

  if [ "$running" -eq 1 ]; then
    # Whole hours are worth announcing; they are the only positive feedback
    # this thing gives.
    local hours=$((current / 3600))
    if [ "$hours" -gt "$notified_hours" ]; then
      notify-send -u normal "Study timer" "$hours hour(s) done."
      [ -r "$WELLDONE_SOUND_FILE" ] && paplay "$WELLDONE_SOUND_FILE" &
      notified_hours=$hours
    fi

    # Running counts as compliance: no reminder is due, and the ladder resets so
    # the next idle stretch starts gently again.
    nag_level=0
    nag_at=0
    return 0
  fi

  # Answered "finished for today": nothing more is said until morning.
  [ "$end_answered" -eq 1 ] && { nag_at=0; return 0; }

  # While the end-of-day question is armed for this stretch it owns the channel.
  # Past half the norm "finished?" is put INSTEAD of "come back", not on top of
  # it -- otherwise the 40-minute nag and the 45-minute question would arrive
  # five minutes apart. Once all $END_ASK_LIMIT have been answered "no", the
  # ordinary ladder comes back -- but never in the same tick as the third
  # question, which is what end_asked_now is for.
  if [ "$end_armed" -eq 1 ] || [ "$end_asked_now" -eq 1 ]; then
    nag_at=0
    return 0
  fi

  # Silent once the day's target is met. A goal you can finish beats pressure
  # that never lets up.
  [ "$current" -ge "$NORM_SECONDS" ] && { nag_at=0; return 0; }

  [ "$(snoozed_until)" -gt 0 ] && return 0
  is_present || return 0

  # First reminder of a stretch: schedule it rather than firing immediately.
  if [ "$nag_at" -eq 0 ]; then
    nag_at=$((NOW + $(nag_interval 0) * 60))
    return 0
  fi

  [ "$NOW" -lt "$nag_at" ] && return 0

  # Present idle, so a reminder never opens with "idle for 1346 min" after a
  # night. That used to be patched by resetting `since` at the rollover; the
  # number is now right by construction instead of by amputation.
  local idle_min=$((idle_present / 60))
  local urgency=normal
  [ "$nag_level" -ge "$NAG_CRITICAL_FROM" ] && urgency=critical

  local left=$(((NORM_SECONDS - current) / 60))
  notify-send -u "$urgency" "Study timer" \
    "Idle for $idle_min min. $((left / 60)) h $((left % 60)) min still to go today."
  [ -r "$DAILY_SOUND_FILE" ] && paplay "$DAILY_SOUND_FILE" &

  nag_level=$((nag_level + 1))
  nag_at=$((NOW + $(nag_interval "$nag_level") * 60))
}

# ------------------------------------------------------------------ report
report() {
  migrate_legacy_log

  local current
  current=$(elapsed)
  printf 'Today      %02d:%02d  (%d%% of %dh)\n' \
    $((current / 3600)) $(((current % 3600) / 60)) \
    $((current * 100 / NORM_SECONDS)) "$NORM_HOURS"

  if [ "$running" -eq 1 ]; then
    # Idle is only defined while the clock is stopped: `since` doubles as the
    # start of the current running segment, and reading it here would print the
    # length of the current session with the opposite meaning.
    printf 'Idle       --:--  (the clock is running)\n'
  else
    local idle=$((NOW - since))
    [ "$idle" -lt 0 ] && idle=0
    printf 'Idle       %02d:%02d  (since %s)\n' \
      $((idle / 3600)) $(((idle % 3600) / 60)) "$(date -d "@$since" +%H:%M)"
  fi

  [ -s "$HISTORY" ] || { echo "No history yet."; return 0; }

  awk -v today="$TODAY" -v now="$current" -v norm="$NORM_SECONDS" '
    { seconds[$1] = $2 }
    END {
      seconds[today] = now

      # Weekly average over the last 7 calendar days, missing days counted as 0.
      cmd = "date -d \"" today "\" +%s"
      cmd | getline base
      close(cmd)

      sum = 0
      for (i = 0; i < 7; i++) {
        cmd = "date -d @" (base - i * 86400) " +%Y-%m-%d"
        cmd | getline d
        close(cmd)
        sum += (d in seconds) ? seconds[d] : 0
      }
      avg = sum / 7
      printf "7-day avg  %02d:%02d\n", int(avg / 3600), int((avg % 3600) / 60)

      # Streak of consecutive days that met the norm, counting back from today.
      # A day still in progress must not read as a broken streak, so today only
      # enters the count once it has actually met the norm.
      streak = 0
      start = ((today in seconds) && seconds[today] >= norm) ? 0 : 1
      for (i = start; i < 3650; i++) {
        cmd = "date -d @" (base - i * 86400) " +%Y-%m-%d"
        cmd | getline d
        close(cmd)
        if ((d in seconds) && seconds[d] >= norm) streak++
        else break
      }
      printf "Streak     %d day(s) at or above the norm\n", streak

      best = 0; bestday = "-"
      for (d in seconds) if (seconds[d] > best) { best = seconds[d]; bestday = d }
      printf "Best       %02d:%02d on %s\n", int(best / 3600), int((best % 3600) / 60), bestday
    }
  ' "$HISTORY"
}

# ------------------------------------------------------------------- usage
usage() {
  cat <<EOF
Study timer — $NORM_HOURS h a day, counted from ${DAY_START_HOUR}:00 to ${DAY_START_HOUR}:00.

  study                 current state, as one line of JSON
  study report          today, 7-day average, streak, best day
  study toggle          start or stop the clock
  study stop            stop, idempotent — safe when already stopped
  study toggle-mode     percentage base: of the target / of the day so far
  study snooze          silence reminders for $SNOOZE_HOURS h, ends with the session
  study unsnooze        cancel a snooze, no token needed
  study pvt             take the vigilance test now
  study pvt report            summary over every recorded run
  study pvt status            mode, protocol, how many runs so far
  study pvt mode operational | training
  study attention       the cancellation test, by hand (not on the timer)
  study attention report      summary over its thirty recorded runs
  study help            this

Reminders arrive while the clock is stopped and you are at the machine, on a
ladder of ${NAG_LADDER[*]} minutes. Idle time means time at the machine with the
clock stopped; time away from it is not idle, it is absence, and is counted
separately. A snooze does not stop the idle count -- you are still sitting
here -- but ending one, by hand or by running out, gives every reminder a full
interval again. Meeting the target is announced once, and
silences them for the rest of the day.
The clock stops itself if the session locks or the screen goes dark, counting
only up to the moment you left.
Past half the norm, every $END_IDLE_MIN idle minutes at the machine it asks
whether the day is over instead of reminding you to come back, at most
$END_ASK_LIMIT times. Saying
yes plays the closing sound now and silences the rest of the day; it does not
close the day, and time worked afterwards still counts towards it.
Every $PVT_INTERVAL_MIN worked minutes the vigilance test is offered, with a
postpone ladder of ${PVT_POSTPONE[*]} minutes; skipping waits for the next
threshold. Starting the clock within $((ANCHOR_FRESH_SECONDS / 60)) min of
returning from $((ANCHOR_IDLE_SECONDS / 3600)) h or more away from the machine
offers it at once instead, as the day's anchor.

Left-click the bar widget to start or stop, right-click to switch the base.
Files live in $ROOT_DIR.
EOF
}

# -------------------------------------------------------------------- main
exec 9>"$LOCK_FILE"
flock 9

load_state
migrate_legacy_log

if [ "$day" != "$TODAY" ]; then
  do_rollover
  save_state
fi

case "${1:-status}" in
  tick)
    # First: the rawest observation, and the only one that also runs while
    # the clock is stopped.
    presence_track
    # Before anything that reads a threshold, so a snooze that has just run
    # out moves the thresholds instead of tripping them.
    snooze_edge
    auto_stop
    # Before reminders, which need to know whether the question is armed.
    end_of_day_check
    reminders
    pvt_check
    save_state
    ;;

  toggle)
    if [ "$running" -eq 1 ]; then
      total=$((total + NOW - since))
      running=0
      idle_before_start=0
      idle_since=$NOW
      idle_present=0
    else
      # Captured before `since` is overwritten. While the clock is stopped
      # `since` is the origin of the idle stretch; the moment it starts, the
      # same field becomes the origin of the running segment and the gap is
      # gone. Note the midnight rollover also resets `since`, so an overnight
      # gap reads as "since midnight" -- an undercount that can only make the
      # anchor offer rarer, never spurious.
      # What preceded this stretch, at the machine. Sleep is not in it: that
      # is `away_gap`, and the two answer different questions -- how long he has
      # been up, and how long he sat here before starting.
      idle_before_start=$idle_present
      idle_present=0
      idle_since=0
      running=1
    fi
    since=$NOW
    nag_level=0
    nag_at=0
    absent_since=0
    save_state
    ;;

  stop)
    if [ "$running" -eq 1 ]; then
      total=$((total + NOW - since))
      running=0
      since=$NOW
      idle_since=$NOW
      idle_present=0
      nag_level=0
      nag_at=0
      absent_since=0
      save_state
    fi
    ;;

  toggle-mode)
    mode=$((mode == 1 ? 0 : 1))
    save_state
    ;;

  snooze)
    token=$(tr -dc 'A-Z0-9' < /dev/urandom | head -c 6)
    printf 'Type %s to silence reminders for %d hours: ' "$token" "$SNOOZE_HOURS"
    read -r answer
    if [ "$answer" != "$token" ]; then
      echo "Mismatch. Reminders stay on."
      exit 1
    fi
    until=$((NOW + SNOOZE_HOURS * 3600))
    echo "$until" > "$SNOOZE_FILE"
    echo "Silent until $(date -d "@$until" +%H:%M). Ends with the session in any case."
    ;;

  unsnooze)
    # No token here on purpose. Friction belongs on the way out of the routine,
    # not on the way back into it.
    snooze_end=$(snoozed_until)
    if [ "$snooze_end" -eq 0 ]; then
      echo "Not snoozed. Reminders are already on."
      exit 0
    fi
    rm -f "$SNOOZE_FILE"

    # The ladder is reset. reminders() returns before the scheduling block while
    # a snooze is active, so nag_at has been sitting in the past for the whole
    # snooze: without this the next tick fires within the minute, at whatever
    # rung the ladder had already reached.
    left=$(((snooze_end - NOW) / 60))
    nag_level=0
    nag_at=0
    # Same for the end-of-day question, which counts present idle and has been
    # counting it right through the snooze. Set here rather than left to
    # snooze_edge so that cancelling by hand and running out come to the same
    # thing; `snooze_was` is cleared so the edge does not fire a second time.
    end_next_idle=$((idle_present + END_IDLE_MIN * 60))
    snooze_was=0
    save_state
    printf 'Reminders back on. %d h %d min of silence cancelled.\n' \
      $((left / 60)) $((left % 60))
    ;;

  pvt)
    # The lock is dropped before anything long-running starts. A manual run
    # takes five minutes of wall clock, and holding the lock through it would
    # freeze the tick exactly as a prompt inside `tick` would.
    shift
    worked=$(($(elapsed) / 60))
    flock -u 9
    case "${1:-run}" in
      run | "")
        exec "$PYTHON_BIN" "$PVT_PY" run --trigger manual --worked "$worked"
        ;;
      report | selftest | legend)
        exec "$PYTHON_BIN" "$PVT_ANALYZE" "$@"
        ;;
      *)
        # mode / status, passed through untouched.
        exec "$PYTHON_BIN" "$PVT_PY" "$@"
        ;;
    esac
    ;;

  pvt-result)
    # Called by pvt/prompt.sh once the offer has been answered. Everything that
    # advances the threshold happens here, under the lock, in one place.
    case "${2:-postpone}" in
      run | skip)
        worked=${3:-$(($(elapsed) / 60))}
        pvt_next_min=$(((worked / PVT_INTERVAL_MIN + 1) * PVT_INTERVAL_MIN))
        pvt_postpone_level=0
        idle_before_start=0
        ;;
      postpone)
        worked=${3:-$(($(elapsed) / 60))}
        pvt_next_min=$((worked + $(pvt_postpone_interval "$pvt_postpone_level")))
        pvt_postpone_level=$((pvt_postpone_level + 1))
        ;;
      *)
        printf 'pvt-result: unknown choice: %s\n' "${2:-}" >&2
        exit 1
        ;;
    esac
    save_state
    ;;

  end-result)
    # Called by bin/session-end.sh once the question has been answered. "no"
    # needs nothing: the next ask was scheduled before the window opened.
    case "${2:-no}" in
      yes)
        end_answered=1
        end_next_idle=0
        nag_level=0
        nag_at=0
        current=$(elapsed)
        notify-send -u critical "End of day" \
          "$((current / 3600)) h $(((current % 3600) / 60)) min today. \
Reminders are off until morning. The clock still works if you come back."
        # No is_present gate, unlike the one at 08:00: a button was just pressed.
        [ -r "$GOODBOY_SOUND" ] && paplay "$GOODBOY_SOUND" &
        ;;
      no) ;;
      *)
        printf 'end-result: unknown choice: %s\n' "${2:-}" >&2
        exit 1
        ;;
    esac
    save_state
    ;;

  attention)
    # The cancellation test. Off the timer since 2026-09-05 -- it measures a real
    # thing (his rate of skipping the exception rule, steady at 24% over 374
    # exceptions) but not fatigue. Kept as a manual command; the thirty recorded
    # sessions stay readable.
    shift
    worked=$(($(elapsed) / 60))
    flock -u 9
    case "${1:-run}" in
      run | "")
        exec "$PYTHON_BIN" "$ATTENTION_PY" run --trigger manual --worked "$worked"
        ;;
      report | selftest | legend)
        exec "$PYTHON_BIN" "$ATTENTION_ANALYZE" "$@"
        ;;
      *)
        exec "$PYTHON_BIN" "$ATTENTION_PY" "$@"
        ;;
    esac
    ;;

  report)
    report
    ;;

  help | -h | --help)
    usage
    ;;

  status)
    current=$(elapsed)
    hours=$((current / 3600))
    minutes=$(((current % 3600) / 60))

    # Written as escapes rather than as the characters themselves: the literal
    # glyphs were silently lost once already in a copy-paste, and a bar showing
    # a bare time with no state icon is not obviously broken.
    #   f04c = pause (running: click to stop)
    #   f04b = play  (stopped: click to start)
    if [ "$running" -eq 1 ]; then
      icon=$'\uf04c'
      class=running
    else
      icon=$'\uf04b'
      class=stopped
    fi

    if [ "$mode" -eq 1 ]; then
      # Share of the day so far, rather than of the target. This one keeps its
      # label: without it the number is meaningless.
      # Measured from the logical day's start, not from midnight, or the
      # percentage would jump at 00:00 in the middle of a working night.
      seconds_today=$((NOW - DAY_STARTED_AT))
      if [ "$seconds_today" -gt 0 ]; then
        percent=$((current * 100 / seconds_today))
      else
        percent=0
      fi
      text=$(printf '%s %02d:%02d / %d%% of day' "$icon" "$hours" "$minutes" "$percent")
    else
      # No "of 10h" suffix: the target is fixed and lives in the tooltip.
      percent=$((current * 100 / NORM_SECONDS))
      text=$(printf '%s %02d:%02d / %d%%' "$icon" "$hours" "$minutes" "$percent")
    fi

    tip="Target ${NORM_HOURS}h"
    snooze_end=$(snoozed_until)
    if [ "$snooze_end" -gt 0 ]; then
      tip="$tip · silenced until $(date -d "@$snooze_end" +%H:%M)"
      class=snoozed
    elif [ "$current" -ge "$NORM_SECONDS" ]; then
      tip="$tip · met"
      class=done
    fi

    # One object per call. The version this replaced printed here and then fell
    # through to a second printf, so two JSON objects went out and the module
    # showed nothing at all.
    # Progress is always measured against the norm, never against the elapsed
    # day: the chip fills to mean one thing, and right-clicking the display mode
    # must not silently redefine it. Capped so the fill cannot overrun its track.
    progress=$((current * 100 / NORM_SECONDS))
    [ "$progress" -gt 100 ] && progress=100

    printf '{"text": "%s", "class": "%s", "tooltip": "%s", "progress": %d}\n' \
      "$text" "$class" "$tip" "$progress"
    ;;

  *)
    # A typo used to fall through to status and look like it had worked.
    printf 'Unknown command: %s\n\n' "$1" >&2
    usage >&2
    exit 1
    ;;
esac
