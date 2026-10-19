import QtQuick
import Quickshell.Io

// Looking up and opening a live log tail: logPathProc resolves where a
// model's logs actually live (see Panel.qml's openLogTerminal()), then
// hands off to root.launchTerminalForLogPath(), which launches terminalProc.
Item {
  required property var root

  property alias logPathProc: logPathProc
  property alias terminalProc: terminalProc

  Process {
    id: logPathProc
    property string pendingHost: ""
    command: []
    stdout: StdioCollector {
      id: logPathOut
      waitForEnd: true
      onStreamFinished: { if (logPathProc.pendingHost === root.activeHost) root.launchTerminalForLogPath(text) }
    }
    stderr: StdioCollector {
      id: logPathErr
      waitForEnd: true
      onStreamFinished: { if (logPathProc.pendingHost === root.activeHost && text.trim() !== "") root.launchTerminalForLogPath("") }
    }
  }

  // Fire-and-forget: the terminal window it opens runs independently of us,
  // so there's nothing to collect here.
  Process {
    id: terminalProc
    command: []
  }
}
