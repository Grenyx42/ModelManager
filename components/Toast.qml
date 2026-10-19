import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Ui
import qs.Commons

// Small self-dismissing toast confirming a registry.json reload actually
// happened, independent of whether the panel above is open - this is the
// whole point, since you're watching the *file*, not necessarily the bar
// popup, while hand-editing it. A dedicated layer-shell surface (like
// omarchy-shell's own OSD) rather than routing through that shared OSD
// singleton, so this plugin doesn't have to reach into shell-owned state
// for something purely its own. Entirely self-contained - nothing in
// Panel.qml references this by id, so it's instantiated with no anchors/
// positioning of its own to set externally.
PanelWindow {
  required property var root
  visible: root.toastVisible
  anchors { top: true; bottom: true; left: true; right: true }
  color: "transparent"
  WlrLayershell.namespace: "modelmanager-toast"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
  exclusionMode: ExclusionMode.Ignore
  // Visual-only surface: keep the layer-shell input region empty so the
  // toast never blocks clicks to the bar/desktop below it.
  mask: Region {}

  BorderSurface {
    id: toastCard
    anchors.top: parent.top
    anchors.right: parent.right
    anchors.topMargin: Style.bar.sizeHorizontal + Style.space(8)
    anchors.rightMargin: Style.space(8)
    width: toastLabel.implicitWidth + Style.space(28)
    height: toastLabel.implicitHeight + Style.space(16)
    color: Util.alpha(Color.background, 0.97)
    borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
    radius: Style.cornerRadius

    Text {
      id: toastLabel
      anchors.centerIn: parent
      text: root.toastText
      color: Color.popups.text
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }
  }
}
