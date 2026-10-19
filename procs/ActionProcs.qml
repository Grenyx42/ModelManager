import QtQuick
import Quickshell.Io

// The user-initiated model/group ensure-stop Processes. Each stamps its own
// pendingHost/pendingName at launch (set by Panel.qml's ensure()/stopModel()/
// ensureGroup()/stopGroup()) and every handler re-checks pendingHost against
// root.activeHost before touching shared state - same stale-response guard
// as DataProcs.qml.
Item {
  required property var root

  property alias ensureProc: ensureProc
  property alias stopProc: stopProc
  property alias groupEnsureProc: groupEnsureProc
  property alias groupStopProc: groupStopProc

  Process {
    id: ensureProc
    property string pendingName: ""
    property string pendingHost: ""
    command: []
    stdout: StdioCollector {
      id: ensureOut
      waitForEnd: true
      onStreamFinished: { if (ensureProc.pendingHost === root.activeHost) root.parseActionResult("ensure", ensureProc.pendingName, text) }
    }
    onExited: (code) => {
      if (ensureProc.pendingHost !== root.activeHost) return
      if (code !== 0 && root.actionError === "") {
        root.actionError = "ensure " + ensureProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }

  Process {
    id: stopProc
    property string pendingName: ""
    property string pendingHost: ""
    command: []
    stdout: StdioCollector {
      id: stopOut
      waitForEnd: true
      onStreamFinished: { if (stopProc.pendingHost === root.activeHost) root.parseActionResult("stop", stopProc.pendingName, text) }
    }
    onExited: (code) => {
      if (stopProc.pendingHost !== root.activeHost) return
      if (code !== 0 && root.actionError === "") {
        root.actionError = "stop " + stopProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }

  Process {
    id: groupEnsureProc
    property string pendingName: ""
    property string pendingHost: ""
    command: []
    stdout: StdioCollector {
      id: groupEnsureOut
      waitForEnd: true
      onStreamFinished: { if (groupEnsureProc.pendingHost === root.activeHost) root.parseGroupActionResult("group-ensure", groupEnsureProc.pendingName, text) }
    }
    onExited: (code) => {
      if (groupEnsureProc.pendingHost !== root.activeHost) return
      if (code !== 0 && root.actionError === "") {
        root.actionError = "group-ensure " + groupEnsureProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }

  Process {
    id: groupStopProc
    property string pendingName: ""
    property string pendingHost: ""
    command: []
    stdout: StdioCollector {
      id: groupStopOut
      waitForEnd: true
      onStreamFinished: { if (groupStopProc.pendingHost === root.activeHost) root.parseGroupActionResult("group-stop", groupStopProc.pendingName, text) }
    }
    onExited: (code) => {
      if (groupStopProc.pendingHost !== root.activeHost) return
      if (code !== 0 && root.actionError === "") {
        root.actionError = "group-stop " + groupStopProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }
}
