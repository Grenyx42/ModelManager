import QtQuick
import Quickshell.Io

// Diagnosis-mode checks: once we know the backend's down but was already
// set up before (see Panel.qml's diagnosisModeActive), these run the
// status/log commands directly instead of just telling the user to run
// them themselves - see checkDiagnosisInfo() and the diagnosis block in
// components/BottomPanel.qml.
Item {
  required property var root

  property alias diagnosisStatusProc: diagnosisStatusProc
  property alias diagnosisLogsProc: diagnosisLogsProc

  // Combines stdout+stderr (2>&1) - `systemctl status` on a unit that
  // exists but isn't running exits non-zero (that's not a failure to
  // report, it's the expected shape of "yes, it's down"), and if the
  // user systemd session itself isn't available for some reason, the
  // error explaining that lands on stderr too - worth showing either way.
  Process {
    id: diagnosisStatusProc
    command: ["sh", "-c", "systemctl --user status modelmanager.service 2>&1"]
    stdout: StdioCollector {
      id: diagnosisStatusOut
      onStreamFinished: { root.diagnosisStatusOutput = text.trim() }
    }
    onExited: (code) => {
      if (diagnosisStatusOut.text.trim() === "") {
        root.diagnosisStatusOutput = "(no output, exit " + code + ")"
      }
    }
  }

  Process {
    id: diagnosisLogsProc
    command: ["sh", "-c", "journalctl --user -u modelmanager.service -n 12 --no-pager 2>&1"]
    stdout: StdioCollector {
      id: diagnosisLogsOut
      onStreamFinished: { root.diagnosisLogsOutput = text.trim() }
    }
    onExited: (code) => {
      if (diagnosisLogsOut.text.trim() === "") {
        root.diagnosisLogsOutput = "(no output, exit " + code + ")"
      }
    }
  }
}
