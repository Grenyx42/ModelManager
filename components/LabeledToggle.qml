import QtQuick
import qs.Ui
import qs.Commons

// A label + qs.Ui's first-party ToggleSwitch, click-anywhere-on-the-row to
// flip - same "row owns the click" pattern qs.Ui's own Toggle uses, just
// without its full-width settings-row chrome (BorderSurface background,
// description line, fixed min-size), since this needs to sit compact and
// inline next to its sibling checkboxes instead of stacked as its own
// settings list. Exists because the setup panel's old approach (a Button
// with a "☑ "/"☐ " glyph glued onto its text) never looked like an actual
// checkbox - this uses the shell's real themed switch control instead.
//
// Root is a plain Item (not the inner Row itself) specifically so the
// click-catching MouseArea below can anchors.fill it - Row refuses
// fill/centerIn/left/right/horizontalCenter anchors on its own children
// since it owns their x positioning itself.
Item {
  id: control

  property bool checked: false
  property string label: ""
  property color foreground: Color.foreground

  signal toggled()

  implicitWidth: row.implicitWidth
  implicitHeight: row.implicitHeight

  Row {
    id: row
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(6)

    ToggleSwitch {
      checked: control.checked
      interactive: false
      trackHeight: Style.space(16)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      text: control.label
      color: control.checked ? Color.accent : control.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      anchors.verticalCenter: parent.verticalCenter
    }
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor
    onClicked: control.toggled()
  }
}
