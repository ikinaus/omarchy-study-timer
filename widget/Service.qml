import QtQuick
import Quickshell
import Quickshell.Io

// Drives the timer's side effects -- reminders, hourly milestones, the midnight
// rollover -- once a minute. Nothing is drawn here; the readout is Timer.qml.
//
// This is the whole point of the plugin declaring a `service` kind alongside its
// bar widget. In waybar the same work rode along inside the bar module's
// `status` call, which meant taking the widget off the bar silently switched the
// reminders off too. A discipline aid that stops working the moment you hide it
// is worse than none.
Scope {
  id: root

  readonly property string script: "$HOME/.config/omarchy/study/timer.sh"

  Process { id: tickProc }

  function tick() {
    tickProc.command = ["bash", "-c", "bash \"" + root.script + "\" tick"]
    if (!tickProc.running) tickProc.running = true
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.tick()
  }
}
