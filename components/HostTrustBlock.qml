import QtQuick
import qs.Ui
import qs.Commons

// Trust-on-first-connect confirmation: shown instead of the Models list
// while root.hostTrustPromptActive is true (see Panel.qml's probeNewHost()/
// confirmHostTrust()/rejectHostTrust() and procs/HostProcs.qml). Positioning
// (anchors/visible) is set where this is instantiated in Panel.qml, since
// that's a layout decision about where this sits in the popup, not
// something this component should own.
Column {
  required property var root
  spacing: Style.space(6)

  Text {
    visible: root.hostProbeRunning
    text: "Connecting to " + root.hostProbePendingName + "..."
    color: root.bar ? root.bar.foreground : Color.foreground
    wrapMode: Text.Wrap
    width: parent.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }

  Text {
    visible: root.hostProbeFingerprint !== ""
    text: "New host " + root.hostProbePendingName + " - certificate fingerprint:\n" + root.hostProbeFingerprint
      + "\nConfirm this matches what that machine's owner told you out-of-band before trusting it."
    color: root.bar ? root.bar.foreground : Color.foreground
    wrapMode: Text.Wrap
    width: parent.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }

  Row {
    visible: root.hostProbeFingerprint !== ""
    spacing: Style.space(8)
    Button {
      text: root.hostAddRunning ? "Trusting..." : "Trust & switch"
      enabled: !root.hostAddRunning
      foreground: enabled ? Color.foreground : Color.muted
      bordered: true
      onClicked: root.confirmHostTrust()
    }
    Button {
      text: "Cancel"
      enabled: !root.hostAddRunning
      foreground: enabled ? Color.foreground : Color.muted
      bordered: true
      onClicked: root.rejectHostTrust()
    }
  }

  Text {
    visible: root.hostProbeError !== ""
    text: root.hostProbeError
    color: Color.urgent
    wrapMode: Text.Wrap
    width: parent.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }
}
