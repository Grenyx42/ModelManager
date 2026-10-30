import QtQuick
import QtQuick.Controls
import qs.Commons

// The shell's default ScrollBar style is a thin, auto-hiding (AsNeeded)
// sliver that's easy to miss - especially on this panel, which stacks
// several independently-scrolling boxes (models, stats, diagnosis
// status/logs) on top of each other. This is a wider, always-visible
// handle instead, dimmed at rest and brightening on hover/press so it
// still reads as a scrollbar rather than a decoration.
ScrollBar {
  id: control
  property color handleColor: Color.foreground

  policy: ScrollBar.AlwaysOn
  implicitWidth: Style.space(9)

  contentItem: Rectangle {
    implicitWidth: Style.space(6)
    radius: width / 2
    color: control.handleColor
    opacity: control.pressed ? 0.75 : (control.hovered ? 0.55 : 0.35)
    Behavior on opacity { NumberAnimation { duration: 100 } }
  }

  background: Rectangle {
    implicitWidth: Style.space(9)
    radius: width / 2
    color: control.handleColor
    opacity: 0.05
  }
}
