import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Bar icon + panel for the ModelManager coordinator. All state comes from
// shelling out to the `modelmanager` CLI, which just talks to the local
// coordinator's REST API (see ../../modelmanager/cli.py) - the same pattern
// the first-party `agents` plugin uses for its usage collectors.
Panel {
  id: root
  moduleName: "wade.modelmanager"
  ipcTarget: "wade.modelmanager"

  // omarchy-shell inherits Hyprland's session environment, not whatever PATH
  // an interactive terminal has after activating the repo's venv - so the
  // bare command "modelmanager" isn't reliably found. Use the venv binary's
  // absolute path instead.
  readonly property string mmBin: Quickshell.env("HOME") + "/dev/ModelManager/.venv/bin/modelmanager"

  property var models: []
  property var systemNames: []
  property int runningCount: 0
  property string lastError: ""
  property string detailLabel: ""
  property string detailText: ""

  readonly property int refreshIntervalSec: {
    var n = parseInt(String(setting("refreshIntervalSec", 15)), 10)
    return isFinite(n) && n > 0 ? n : 15
  }

  function refresh() {
    if (!listProc.running) listProc.running = true
    if (!systemsProc.running) systemsProc.running = true
  }

  function ensure(name) {
    if (ensureProc.running) return
    ensureProc.command = [root.mmBin, "ensure", name]
    ensureProc.running = true
  }

  function stopModel(name) {
    if (stopProc.running) return
    stopProc.command = [root.mmBin, "stop", name]
    stopProc.running = true
  }

  function showLogs(name) {
    if (logsProc.running) return
    detailLabel = "logs: " + name
    detailText = "loading..."
    logsProc.command = [root.mmBin, "logs", name, "--lines", "80"]
    logsProc.running = true
  }

  function showStats(system) {
    if (statsProc.running) return
    detailLabel = "stats: " + system
    detailText = "loading..."
    statsProc.command = [root.mmBin, "stats", system]
    statsProc.running = true
  }

  function parseModels(text) {
    try {
      var data = JSON.parse(text)
      models = data
      var count = 0
      for (var i = 0; i < data.length; i++) if (data[i].active) count += 1
      runningCount = count
      lastError = ""
    } catch (e) {
      lastError = "bad /v1/models response"
    }
  }

  function parseSystems(text) {
    try {
      var data = JSON.parse(text)
      systemNames = Object.keys(data)
    } catch (e) {
      // leave systemNames as-is; systems list is secondary to model status
    }
  }

  function statusColor(model) {
    if (model.active) return Color.accent
    return Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.6)
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: listProc
    command: [root.mmBin, "list"]
    stdout: StdioCollector {
      id: listOut
      waitForEnd: true
      onStreamFinished: root.parseModels(text)
    }
    stderr: StdioCollector {
      id: listErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.lastError = text.trim() }
    }
  }

  Process {
    id: systemsProc
    command: [root.mmBin, "systems"]
    stdout: StdioCollector {
      id: systemsOut
      waitForEnd: true
      onStreamFinished: root.parseSystems(text)
    }
  }

  Process {
    id: ensureProc
    command: []
    onExited: root.refresh()
  }

  Process {
    id: stopProc
    command: []
    onExited: root.refresh()
  }

  Process {
    id: logsProc
    command: []
    stdout: StdioCollector { id: logsOut; waitForEnd: true; onStreamFinished: root.detailText = text }
    stderr: StdioCollector { id: logsErr; waitForEnd: true; onStreamFinished: { if (text.trim() !== "") root.detailText = text } }
  }

  Process {
    id: statsProc
    command: []
    stdout: StdioCollector { id: statsOut; waitForEnd: true; onStreamFinished: root.detailText = text }
    stderr: StdioCollector { id: statsErr; waitForEnd: true; onStreamFinished: { if (text.trim() !== "") root.detailText = text } }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "🧠 " + root.runningCount
    slotSize: Style.bar.iconSlot
    tooltipText: root.lastError !== "" ? root.lastError : (root.runningCount + " model(s) running")
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        PanelSectionHeader { text: "Models" }

        Repeater {
          model: root.models
          delegate: Item {
            width: column.width
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
                  visible: modelData.candidate !== null && modelData.candidate !== undefined
                  text: modelData.candidate ? (modelData.candidate.engine + " @ " + modelData.candidate.host + ":" + modelData.candidate.port) : ""
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
                onClicked: root.showLogs(modelData.name)
              }

              Button {
                id: ensureBtn
                text: modelData.active ? "Stop" : "Ensure"
                bordered: true
                onClicked: modelData.active ? root.stopModel(modelData.name) : root.ensure(modelData.name)
              }
            }
          }
        }

        PanelSeparator {}
        PanelSectionHeader { text: "Systems" }

        Repeater {
          model: root.systemNames
          delegate: Row {
            width: column.width
            spacing: Style.space(8)
            Text {
              text: modelData
              color: root.bar ? root.bar.foreground : Color.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              width: parent.width - statsBtn.implicitWidth - Style.space(16)
            }
            Button {
              id: statsBtn
              text: "Stats"
              bordered: true
              onClicked: root.showStats(modelData)
            }
          }
        }

        PanelSeparator { visible: root.detailLabel !== "" }

        Column {
          visible: root.detailLabel !== ""
          width: column.width
          spacing: Style.space(4)

          PanelSectionHeader { text: root.detailLabel }
          Text {
            text: root.detailText
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
            width: parent.width
          }
        }

        Text {
          visible: root.lastError !== ""
          text: root.lastError
          color: Color.urgent
          wrapMode: Text.Wrap
          width: column.width
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
