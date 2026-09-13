import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui

// Display only. Every side effect lives in Service.qml, which runs `tick` on its
// own schedule, so this widget can be taken off the bar without silencing the
// reminders. Clicks are the one thing that writes, and timer.sh holds an flock
// so a click can never collide with a tick.
BarWidget {
  id: root
  moduleName: "mordrud.timer"

  // Expanded by the shell, so $HOME needs no lookup in QML. Called through
  // `bash` rather than directly: the executable bit does not survive a copy
  // through the file bridge, and this makes that irrelevant.
  readonly property string script: String(
    root.setting("script", "$HOME/.config/omarchy/study/timer.sh")
  )

  // The display only shows whole minutes, so polling faster would spawn
  // processes for nothing.
  readonly property int pollSeconds: Number(root.setting("pollSeconds", 60))

  property string label: ""
  property string tip: "Study timer"
  property string state: "stopped"
  property int progress: 0

  // Chip behind the readout. Its job is to lift the timer out of the row of
  // equals: everything else in the bar is a bare glyph or a bare number on the
  // bar's own ground, so a filled shape reads as a separate object without
  // moving anything. Kept faint -- against #05182e even 0.08 of the foreground
  // is clearly visible, and anything stronger competes with the text it holds.
  readonly property real chipAlpha: Number(root.setting("chipAlpha", 0.08))
  readonly property real chipInset: Style.space(4)

  function run(action, process) {
    process.command = ["bash", "-c", "bash \"" + root.script + "\" " + action]
    if (!process.running) process.running = true
  }

  function refresh() {
    root.run("status", statusProc)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: statusProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed

        try {
          parsed = JSON.parse(text || "{}")
        } catch (error) {
          // A malformed line means the script broke; keep the last good value on
          // screen rather than blanking the widget.
          return
        }

        root.label = String(parsed.text || "")
        root.state = String(parsed.class || "stopped")
        root.tip = String(parsed.tooltip || "Study timer")
        root.progress = Number(parsed.progress || 0)
      }
    }
  }

  // Kept separate from statusProc so a click never races the poll, and so its
  // exit can trigger an immediate re-read instead of waiting out the interval.
  Process {
    id: actionProc
    onRunningChanged: if (!running) root.refresh()
  }

  Timer {
    interval: root.pollSeconds * 1000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Component.onCompleted: root.refresh()

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.label
    // A readout that changes every minute needs tabular figures -- equal advance
    // width for every digit -- or the whole widget twitches as the minutes roll
    // over and drags its neighbours with it. That rules out display serifs:
    // Cinzel was tried here and its proportional figures were the problem, not
    // its shape.
    //
    // Nimbus Mono PS is URW's Courier clone: monospaced, so the figures are
    // tabular by construction, and serifed, which keeps some of the character
    // Cinzel was reached for. Already installed via gsfonts.
    //
    // Drop this line to fall back to the bar's own face (Cartograph CF).
    fontFamily: "Nimbus Mono PS"
    // The bar's urgent colour while the clock runs, so it follows the theme
    // instead of a hardcoded value.
    active: root.state === "running"
    // Target met or reminders silenced: still legible, visibly not demanding
    // anything.
    dimmed: root.state === "done" || root.state === "snoozed"
    horizontalMargin: 8
    verticalPadding: 6

    // z below zero puts this under WidgetButton's own label, which is declared
    // inside the component and would otherwise paint first.
    Rectangle {
      anchors.fill: parent
      anchors.topMargin: root.chipInset
      anchors.bottomMargin: root.chipInset
      z: -1
      radius: Style.cornerRadius
      // clip so the fill below can be square-edged and still come out with the
      // chip's rounded corners -- a rounded fill looks like a pill at low
      // values and like nothing at all at high ones.
      clip: true
      // Tinted by state, so the chip carries the running/stopped signal too and
      // the text does not have to shout it alone.
      color: root.state === "running"
        ? Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, root.chipAlpha * 1.6)
        : Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, root.chipAlpha)

      // The chip doubles as the gauge: it fills left to right toward the daily
      // norm. Continuous rather than the ten-step battery ramp the old waybar
      // config used for charge -- no quantisation, no extra glyph in the text,
      // and it costs nothing because the track was already there.
      Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: parent.width * Math.max(0, Math.min(100, root.progress)) / 100
        color: root.state === "running"
          ? Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, root.chipAlpha * 3.4)
          : Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, root.chipAlpha * 2.4)

        // The minute tick would otherwise make the edge jump; a slow ease reads
        // as filling rather than as redrawing.
        Behavior on width {
          NumberAnimation { duration: 400; easing.type: Easing.OutCubic }
        }
      }
    }
    tooltipText: root.tip + "  •  left: start/stop  •  right: switch % base"
    onPressed: function(mouse) {
      root.run(mouse === Qt.RightButton ? "toggle-mode" : "toggle", actionProc)
    }
  }
}
