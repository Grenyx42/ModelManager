import QtQuick
import Quickshell.Io

// First-run setup flow: detecting an unconfigured registry, running
// scripts/setup.sh, and the two optional one-click extras (linking a coding
// agent's skill, registering an existing Ollama model) offered alongside
// it. See Panel.qml's startSetupAction()/checkFreshInstall() and the
// first-run guidance block in components/BottomPanel.qml.
Item {
  required property var root

  property alias registryDefaultCheckProc: registryDefaultCheckProc
  property alias setupProc: setupProc
  property alias skillLinkProc: skillLinkProc
  property alias ollamaRegisterProc: ollamaRegisterProc
  // Exposed so root.setupLiveOutput (a plain property binding, not a
  // Process-internal one) can keep reading them live while setupProc runs.
  property alias setupOut: setupOut
  property alias setupErr: setupErr

  Process {
    id: registryDefaultCheckProc
    command: []
    onExited: (code) => { root.registryIsDefault = (code === 0) }
  }

  Process {
    id: setupProc
    // waitForEnd: false (unlike every other StdioCollector in this file) -
    // confirmed directly: true buffers everything and fires dataChanged
    // exactly once at the very end, same timing as streamFinished; false
    // fires it per chunk as output actually arrives, which is what lets
    // the live-output Text below update in real time while this runs
    // instead of jumping straight from nothing to the final result.
    stdout: StdioCollector { id: setupOut; waitForEnd: false }
    stderr: StdioCollector {
      id: setupErr
      waitForEnd: false
      onStreamFinished: { if (text.trim() !== "") root.setupResult = text.trim() }
    }
    onExited: (code) => {
      root.setupRunning = false
      if (code === 0) {
        // Overrides any benign stderr noise above - a zero exit from
        // setup.sh's own `set -e` means it actually finished successfully.
        root.setupResult = "ok"
        root.refresh()
        root.checkFreshInstall()
        root.applyPendingSkillLinks()
        root.applyPendingOllamaRegistration()
      } else if (root.setupResult === "") {
        root.setupResult = "setup.sh failed (exit " + code + ")"
      }
    }
  }

  Process {
    id: skillLinkProc
    // Any stderr here means mkdir/ln itself failed (bad permissions, a
    // same-named non-symlink already there) - genuinely unexpected, since
    // skillDirPath always exists (it ships with the plugin) and the
    // destination is always a path this user owns.
    property string lastStderr: ""
    stderr: StdioCollector {
      id: skillLinkErr
      waitForEnd: true
      onStreamFinished: { skillLinkProc.lastStderr = text.trim() }
    }
    onExited: (code) => {
      root.skillLinkRunning = false
      root.skillLinkDoneList = root.skillLinkDoneList.concat([
        root.skillLinkCurrentAgent + ": " + (code === 0
          ? "linked"
          : "failed" + (skillLinkProc.lastStderr !== "" ? " (" + skillLinkProc.lastStderr + ")" : " (exit " + code + ")"))
      ])
      skillLinkProc.lastStderr = ""
      root.processSkillLinkQueue()
    }
  }

  Process {
    id: ollamaRegisterProc
    stdout: StdioCollector { id: ollamaRegisterOut; waitForEnd: true }
    stderr: StdioCollector {
      id: ollamaRegisterErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.ollamaRegisterResult = text.trim() }
    }
    onExited: (code) => {
      root.ollamaRegisterRunning = false
      if (code === 0) root.ollamaRegisterResult = ollamaRegisterOut.text.trim()
      else if (root.ollamaRegisterResult === "") root.ollamaRegisterResult = "failed (exit " + code + ")"
    }
  }
}
