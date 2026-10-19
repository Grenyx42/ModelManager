import QtQuick
import Quickshell.Io

// The panel's read-only polling Processes: everything that just fetches
// data on a timer/refresh and parses it into a root property, with no
// user-initiated action of its own. Grouped together (rather than one file
// per Process) since they're all the same shape and all driven by the same
// refresh()/startStatsSweep() call sites in Panel.qml. Exposed back to
// Panel.qml via property alias, under the exact same ids the Process blocks
// used to have inline - every `listProc.running`/`pendingHost` call site in
// Panel.qml's own functions keeps working unchanged.
Item {
  required property var root

  property alias listProc: listProc
  property alias systemsProc: systemsProc
  property alias gpuGroupsProc: gpuGroupsProc
  property alias groupsProc: groupsProc
  property alias statsSweepProc: statsSweepProc
  property alias reloadStatusProc: reloadStatusProc

  Process {
    id: listProc
    // See refresh()'s own comment: pendingHost is stamped with
    // root.activeHost at launch, and every handler below checks it's still
    // current before touching any state - a request that was in flight for
    // a previous host (refresh() couldn't start a fresh one on top of it)
    // must not clobber state for whatever's active by the time it finally
    // lands.
    property string pendingHost: ""
    command: root.mmCommand(["list"])
    stdout: StdioCollector {
      id: listOut
      waitForEnd: true
      onStreamFinished: { if (listProc.pendingHost === root.activeHost) root.parseModels(text) }
    }
    stderr: StdioCollector {
      id: listErr
      waitForEnd: true
      onStreamFinished: { if (listProc.pendingHost === root.activeHost && text.trim() !== "") root.lastError = text.trim() }
    }
    // A missing/unreadable mmBin (venv never set up - see scripts/setup.sh)
    // now reaches here as a normal nonzero exit via mmCommand()'s sh
    // wrapper (see its own comment) - sh's own "command not found"/"not
    // executable" exit codes (127/126) always mean mmBin itself is the
    // problem, so they take priority and overwrite lastError even if sh's
    // raw stderr ("No such file or directory") already landed there first;
    // without this, the panel would silently sit empty forever with no
    // indication why.
    onExited: (code) => {
      if (listProc.pendingHost !== root.activeHost) return
      if (code === 126 || code === 127 || (code !== 0 && root.lastError === "")) {
        root.lastError = "can't run " + root.mmBin + " - run this repo's scripts/setup.sh, or set MODELMANAGER_BIN if installed elsewhere"
        root.checkFreshInstall()
      }
    }
  }

  Process {
    id: systemsProc
    property string pendingHost: ""
    command: root.mmCommand(["systems"])
    stdout: StdioCollector {
      id: systemsOut
      waitForEnd: true
      onStreamFinished: { if (systemsProc.pendingHost === root.activeHost) root.parseSystems(text) }
    }
  }

  Process {
    id: gpuGroupsProc
    property string pendingHost: ""
    command: root.mmCommand(["config"])
    stdout: StdioCollector {
      id: gpuGroupsOut
      waitForEnd: true
      onStreamFinished: { if (gpuGroupsProc.pendingHost === root.activeHost) root.parseGpuGroupSystems(text) }
    }
  }

  Process {
    id: groupsProc
    property string pendingHost: ""
    command: root.mmCommand(["groups"])
    stdout: StdioCollector {
      id: groupsOut
      waitForEnd: true
      onStreamFinished: { if (groupsProc.pendingHost === root.activeHost) root.parseGroups(text) }
    }
  }

  Process {
    id: statsSweepProc
    property string pendingHost: ""
    command: []
    stdout: StdioCollector {
      id: statsSweepOut
      waitForEnd: true
      onStreamFinished: { if (statsSweepProc.pendingHost === root.activeHost) root.parseAllSystemStats(text) }
    }
  }

  Process {
    id: reloadStatusProc
    command: root.mmCommand(["reload-status"])
    stdout: StdioCollector { id: reloadStatusOut; waitForEnd: true; onStreamFinished: root.parseReloadStatus(text) }
  }
}
