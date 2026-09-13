# study

A work timer for [Omarchy](https://omarchy.org) (Hyprland + Quickshell), with a
vigilance test attached to it.

The timer counts working time against a daily target, nags while the clock is
stopped, and stops itself when the machine is left alone. Every 90 worked
minutes it offers a five-minute reaction-time test, so that "I am tired" can be
checked against a number instead of a feeling.

Written for one machine and one person. It is published because the reasoning in
the comments may be useful, not because it is a product: paths, sounds, window
rules and the Russian-language test UI are all specific to that setup.

## Parts

```
timer.sh                the timer itself; every verb goes through it
bin/session-end.{sh,py} the end-of-day question
pvt/                    the vigilance test (PVT-B): UI, storage, analysis
attention/              a letter-cancellation test, kept but off the trigger
assets/                 notification sounds
widget/                 the Quickshell bar plugin (see Install)
hypr/                   window rules to paste into your Hyprland config
data/                   created on first run; never committed
```

The clock is a single bash script holding an `flock` on its state file. Nothing
long-running ever happens under that lock: prompts and tests are launched
detached and report back through a separate verb, because a lock held for the
five minutes of a test would freeze the tick and the bar widget with it.

## Requires

- bash, `flock`, `setsid` (util-linux)
- Python 3 with PyGObject and GTK 4 — on Arch, `python-gobject` and `gtk4`.
  The scripts call `/usr/bin/python3` explicitly: a virtualenv cannot see a
  system GTK binding, and `python3` on this machine is a virtualenv.
- `notify-send`, `paplay`
- `hyprctl` and `loginctl` for presence detection. Both are optional; each probe
  only votes "away" when it is installed and actually says so, because for a
  discipline aid failing quiet is worse than a stray notification.

## Install

```sh
git clone <this repo> ~/.config/omarchy/study
ln -s ~/.config/omarchy/study/widget ~/.config/omarchy/plugins/mordrud.timer
alias study='bash ~/.config/omarchy/study/timer.sh'
```

Paths inside the tree derive from the script's own location, so it can live
anywhere; the two lines above are the only places the location is written down.
Then paste `hypr/windows.lua.snippet` into your Hyprland window rules — without
it the test windows are tiled, and a stimulus that appears wherever the layout
left room is not a stimulus you can time reactions to.

Reminders are driven by the Quickshell plugin's `service`, which runs
`timer.sh tick` once a minute. That cadence is load-bearing: idle time is
accumulated tick by tick, and a hole in the sequence is how the script knows the
machine was suspended.

## Commands

```
study                 current state, as one line of JSON
study report          today, 7-day average, streak, best day
study toggle          start or stop the clock
study stop            stop, idempotent
study snooze          silence reminders for two hours; asks for a token back
study unsnooze        cancel a snooze, no token
study pvt             take the vigilance test now
study pvt report      summary over every recorded run
study attention       the cancellation test, by hand
study help            the rest
```

## Three quantities, three definitions

The distinction the script is built around, and the one it got wrong for a
while:

| | |
|---|---|
| **worked** | the clock is running |
| **idle** | at the machine, clock stopped |
| **absence** | not at the machine at all |

Idle used to mean wall-clock time since the last stop, which read a night's
sleep as 1346 minutes of idleness. It is now accumulated only while the machine
is attended and the clock is not; absence is measured separately, from the
presence probes and from gaps in the tick sequence. Reminders count idle; the
rested-baseline rule for the test counts absence.

## The vigilance test

A 5-minute PVT-B: a millisecond counter appears at a random interval of 1–4 s,
you press as fast as you can, the counter is also the feedback. Five minutes
rather than the published three because the shortest version is weakest exactly
on the rested baseline every later measurement is divided by.

Reaction time is taken from the input event's own timestamp against the frame's
real presentation time, not from a Python clock read in the handler.
`pvt/timing_probe.py` measures whether that is possible on a given machine
before trusting any of it.

It replaced the letter-cancellation test in `attention/`, which is kept and
still runnable. That test measures something real, but not fatigue: it is
self-paced, so the subject picks his own point on the speed–accuracy tradeoff
and moves along it freely, and the summary score is built to be invariant along
exactly that line. Two runs at identical worked time came out opposite — fast
and sloppy, slow and near-perfect — and the score could not tell them apart.

## Licence

Do what you like with the code. The sounds in `assets/` are not mine to
relicense.
