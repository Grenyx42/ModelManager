import QtQuick
import QtQuick.Controls
import qs.Ui
import qs.Commons

// The Models list: registry.json's named groups (one-click "ensure every
// member") followed by every model, grouped by gpu_group. Positioning
// (anchors/visible) is set where this is instantiated in Panel.qml, since
// it depends on sibling ids (modelsHeaderRow, bottomBlock) that live there.
Flickable {
  id: modelsFlick
  required property var root
  contentWidth: width
  contentHeight: modelsColumn.implicitHeight
  clip: true
  boundsBehavior: Flickable.StopAtBounds
  flickableDirection: Flickable.VerticalFlick
  interactive: contentHeight > height
  ScrollBar.vertical: PanelScrollBar { handleColor: root.bar ? root.bar.foreground : Color.foreground }

  // Plain Flickable only grabs the wheel when it's actually scrollable
  // (interactive above) and stops grabbing it once scrolled to its own
  // top/bottom edge. Trapping the wheel here unconditionally - whether
  // or not there's anything left to scroll - keeps a stray wheel event
  // over this list from leaking anywhere else, which matters more now
  // that these two lists are the only things that scroll at all.
  WheelHandler {
    target: null
    onWheel: (event) => {
      var maxY = Math.max(0, modelsFlick.contentHeight - modelsFlick.height)
      modelsFlick.contentY = Math.max(0, Math.min(maxY, modelsFlick.contentY - event.angleDelta.y))
      event.accepted = true
    }
  }

  Column {
    id: modelsColumn
    width: modelsFlick.width
    spacing: Style.space(10)

    // Named model sets from registry.json's "groups" - one click to
    // bring up a whole working set (see Manager.ensure_group). They
    // share this list (and its filter) with the models rather than
    // living in their own section, and the "GRP:" prefix is what
    // tells them apart from the gpu_group headers below - which are a
    // different concept entirely (which models contend for which
    // physical GPU) that happens to share the word "group".
    Repeater {
      model: root.filteredGroups

      delegate: Column {
        id: groupEntry
        width: modelsColumn.width
        spacing: Style.space(4)

        readonly property var st: root.groupState(modelData)
        readonly property bool allUp: st.total > 0 && st.active === st.total
        readonly property bool starting: root.groupEnsureProc.running && root.groupEnsureProc.pendingName === modelData.name
        readonly property bool stopping: root.groupStopProc.running && root.groupStopProc.pendingName === modelData.name

        // Anchor-based rather than a Row whose children's widths are
        // supposed to sum to exactly parent.width - that arithmetic left
        // zero margin on the right (confirmed: 8 (dot) + 2*Style.space(8)
        // (the Row's own inter-child spacing) + this Text's width summed
        // to precisely parent.width), so groupActionBtn's right edge sat
        // flush against - and clipped past - the card's edge, and didn't
        // line up with the Ensure/Stop buttons on the model rows below
        // (those reserve a real margin - Style.space(80) in a similar
        // sum-to-parent.width formula that works out to ~Style.space(48)
        // of actual right margin, matched here directly instead of
        // re-deriving it from an unrelated row's arithmetic again).
        Item {
          width: parent.width
          implicitHeight: Math.max(groupDot.implicitHeight, groupActionBtn.implicitHeight, groupNameText.implicitHeight)
          height: implicitHeight

          Rectangle {
            id: groupDot
            width: Style.space(8)
            height: Style.space(8)
            radius: width / 2
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            color: root.groupStatusColor(groupEntry.st)
          }

          Text {
            id: groupNameText
            textFormat: Text.PlainText
            anchors.left: groupDot.right
            anchors.leftMargin: Style.space(8)
            anchors.right: groupActionBtn.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: "GRP:" + modelData.name
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            font.bold: true
            elide: Text.ElideRight
          }

          // One toggle button, same as a single model's row: "Stop"
          // once every member is up, "Ensure" otherwise (brings up
          // whatever's missing) - same best-effort-per-member
          // ensure_group()/group-stop semantics either way, just one
          // button standing in for "the group's not fully up yet" vs.
          // "it is."
          Button {
            id: groupActionBtn
            anchors.right: parent.right
            anchors.rightMargin: Style.space(48)
            anchors.verticalCenter: parent.verticalCenter
            text: groupEntry.starting ? "Starting..." : (groupEntry.stopping ? "Stopping..." : (groupEntry.allUp ? "Stop" : "Ensure"))
            enabled: !root.groupEnsureProc.running && !root.groupStopProc.running
            foreground: enabled ? Color.foreground : Color.muted
            bordered: true
            onClicked: groupEntry.allUp ? root.stopGroup(modelData.name) : root.ensureGroup(modelData.name)
          }
        }

        // Members, indented under their group, so a group isn't an
        // opaque name you have to go read registry.json to decode.
        // Flow wraps rather than eliding - a group with six members
        // shouldn't hide five of them.
        Flow {
          x: Style.space(16)
          width: parent.width - Style.space(16)
          spacing: Style.space(10)

          Repeater {
            model: modelData.models

            delegate: Row {
              spacing: Style.space(4)

              Rectangle {
                width: Style.space(6)
                height: Style.space(6)
                radius: width / 2
                anchors.verticalCenter: parent.verticalCenter
                // modelData here is the member's name (a string) - the
                // inner Repeater shadows the group delegate's modelData.
                color: root.memberStatusColor(modelData)
              }

              Text {
                textFormat: Text.PlainText
                text: root.memberLabel(modelData)
                color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
          }
        }
      }
    }

    Repeater {
      model: root.filteredGroupedModels
      delegate: Column {
        width: modelsColumn.width
        spacing: Style.space(4)

        PanelSectionHeader { text: modelData.group }

        Repeater {
          model: modelData.models
          delegate: Item {
            width: modelsColumn.width
            implicitHeight: modelRow.implicitHeight

            Row {
              id: modelRow
              width: parent.width
              spacing: Style.space(8)

              Rectangle {
                width: Style.space(8)
                height: Style.space(8)
                radius: width / 2
                anchors.verticalCenter: parent.verticalCenter
                color: root.statusColor(modelData)
              }

              Column {
                width: parent.width - ensureBtn.implicitWidth - logsBtn.implicitWidth - Style.space(80)
                Text {
                  text: modelData.name + (modelData.pinned ? "  • pinned" : "")
                  color: root.bar ? root.bar.foreground : Color.foreground
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  elide: Text.ElideRight
                  width: parent.width
                }
                Text {
                  visible: modelData.active
                  text: modelData.active ? (modelData.candidate.engine + " @ " + modelData.candidate.host + ":" + modelData.candidate.port) : ""
                  color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                  width: parent.width
                }
              }

              Button {
                id: logsBtn
                text: "Logs"
                bordered: true
                onClicked: root.openLogTerminal(modelData.name)
              }

              Button {
                id: ensureBtn
                // A start can take a while (model load, or an SSH
                // round-trip to a remote box), and modelData.active
                // only flips once the CLI returns and refresh() lands
                // - so the button used to sit there still saying
                // "Ensure" the whole time, looking like the click did
                // nothing, before abruptly becoming "Stop".
                readonly property bool starting: root.ensureProc.running && root.ensureProc.pendingName === modelData.name
                text: starting ? "Starting..." : (modelData.active ? "Stop" : "Ensure")
                // ensure() drops a second call while one is in flight,
                // so every other inactive model's button is a dead
                // click too until this one finishes - telling the same
                // lie we're fixing here. Stop stays live throughout:
                // it runs on its own process, independent of this one.
                enabled: !(root.ensureProc.running && !modelData.active)
                foreground: enabled ? Color.foreground : Color.muted
                bordered: true
                onClicked: modelData.active ? root.stopModel(modelData.name) : root.ensure(modelData.name)
              }
            }
          }
        }
      }
    }
  }
}
