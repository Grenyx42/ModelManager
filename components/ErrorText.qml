import QtQuick
import QtQuick.Controls
import qs.Ui
import qs.Commons

// A plain red status/error line that caps itself instead of growing
// without bound. BottomPanel.qml's Column (bottomBlock) is pinned to the
// bottom of a fixed-height popup with no outer clip (see Panel.qml's own
// "contentHolder ... doesn't clip its children" comment) - an unusually
// long backend-produced message (e.g. a remote port-check's full stderr
// tail, see process.py's PortInUseError) used to grow bottomBlock's
// implicitHeight past the popup's fixed height, pushing it up over the
// Models list and header above instead of scrolling. Math.min defers to
// the label's own implicitHeight until content actually overflows, so the
// common one-line case renders exactly as a plain Text would.
Flickable {
  id: root
  property string text: ""
  property color textColor: Color.urgent
  visible: text !== ""
  width: parent.width
  height: Math.min(Style.space(90), contentHeight)
  contentWidth: width
  contentHeight: label.implicitHeight
  clip: true
  boundsBehavior: Flickable.StopAtBounds
  flickableDirection: Flickable.VerticalFlick
  interactive: contentHeight > height
  // Plain AsNeeded, not the always-visible PanelScrollBar the nested
  // diagnosis/setup log boxes use - same reasoning as setupGuidanceFlick's
  // own outer Flickable in BottomPanel.qml: this is a rare-case overflow
  // safety net, not a primary scroll surface.
  ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

  WheelHandler {
    target: null
    onWheel: (event) => {
      var maxY = Math.max(0, root.contentHeight - root.height)
      root.contentY = Math.max(0, Math.min(maxY, root.contentY - event.angleDelta.y))
      event.accepted = true
    }
  }

  Text {
    id: label
    text: root.text
    color: root.textColor
    wrapMode: Text.Wrap
    width: root.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }
}
