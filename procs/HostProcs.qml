import QtQuick
import Quickshell.Io

// Trust-on-first-connect host management: listing/probing/trusting/touching
// a remote coordinator by name (see Panel.qml's probeNewHost()/
// confirmHostTrust()/touchKnownHost() and known_hosts.json on the backend).
// A failed probe/add resets the host field back to the currently-active
// host via root.resetHostField() rather than touching hostField directly -
// that TextField lives in Panel.qml's own visual tree, not here.
Item {
  required property var root

  property alias hostsListProc: hostsListProc
  property alias hostsProbeProc: hostsProbeProc
  property alias hostsAddProc: hostsAddProc
  property alias hostTouchProc: hostTouchProc

  Process {
    id: hostsListProc
    command: root.mmLocalHostsCommand(["hosts", "list"])
    stdout: StdioCollector {
      id: hostsListOut
      waitForEnd: true
      onStreamFinished: root.parseKnownHosts(text)
    }
  }

  // Trust-on-first-connect probe: fetches the candidate coordinator's cert
  // fingerprint without saving anything (mirrors `modelmanager hosts
  // probe`) - confirmHostTrust()/hostsAddProc below is the only thing that
  // actually persists it, and only after the fingerprint's been shown.
  Process {
    id: hostsProbeProc
    command: []
    stdout: StdioCollector { id: hostsProbeOut; waitForEnd: true }
    stderr: StdioCollector {
      id: hostsProbeErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.hostProbeError = text.trim() }
    }
    onExited: (code) => {
      root.hostProbeRunning = false
      if (code !== 0) {
        if (root.hostProbeError === "") root.hostProbeError = "couldn't reach " + root.hostProbePendingName
        root.hostProbePendingName = ""
        root.resetHostField()
        return
      }
      try {
        var data = JSON.parse(hostsProbeOut.text)
        if (data && data.error) {
          root.hostProbeError = data.error
          root.hostProbePendingName = ""
          root.resetHostField()
        } else {
          root.hostProbeFingerprint = (data && data.fingerprint) || ""
        }
      } catch (e) {
        root.hostProbeError = "bad response from hosts probe"
        root.hostProbePendingName = ""
        root.resetHostField()
      }
    }
  }

  Process {
    id: hostsAddProc
    command: []
    stdout: StdioCollector { id: hostsAddOut; waitForEnd: true }
    stderr: StdioCollector {
      id: hostsAddErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.hostProbeError = text.trim() }
    }
    onExited: (code) => {
      root.hostAddRunning = false
      if (code === 0) {
        var name = root.hostProbePendingName
        root.hostProbeFingerprint = ""
        root.hostProbePendingName = ""
        root.hostProbeError = ""
        root.knownHostNames = root.knownHostNames.concat([name])
        root.switchHost(name)
      } else {
        if (root.hostProbeError === "") root.hostProbeError = "failed to trust host (exit " + code + ")"
        root.resetHostField()
      }
    }
  }

  // Fire-and-forget (see touchKnownHost) - nothing reads its output.
  Process {
    id: hostTouchProc
    command: []
  }
}
