import QtQuick
import QtQuick.Controls
import qs.Ui
import qs.Commons

// Everything that lives below the model list: action/registry errors, the
// first-run setup guidance (shown while the backend's down or the registry
// is still the untouched clean default), and the Stats section. Positioning
// (anchors) is set where this is instantiated in Panel.qml, since pinning
// this to the bottom of the card is a layout decision about its sibling
// (modelsFlick anchors its own bottom to this one's top), not something
// this component should own.
Column {
  required property var root
  spacing: Style.space(10)

  Text {
    visible: root.actionError !== ""
    text: root.actionError
    color: Color.urgent
    wrapMode: Text.Wrap
    width: parent.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }

  Text {
    visible: root.registryError !== ""
    text: "registry.json failed to reload: " + root.registryError
    color: Color.urgent
    wrapMode: Text.Wrap
    width: parent.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }

  // Shown whenever the backend is down, or the registry is still the
  // untouched clean default (independent conditions - OR, not AND -
  // so this survives the backend coming up if nobody's described a
  // model yet). Reads top to bottom on purpose: status, then what
  // the button will actually do, then the skill choice to make
  // *before* clicking it, then the button itself last.
  //
  // Gated on activeHost === "" too: setup.sh/skill-linking/Ollama
  // registration all act on *this* machine's own install - none of
  // that is the right response to a remote peer being unreachable or
  // never having had a model registered, so none of it should show
  // while targeting one.
  Column {
    visible: root.activeHost === "" && (root.backendUnreachable || root.registryIsDefault)
    width: parent.width
    spacing: Style.space(6)

    Text {
      text: root.backendUnreachable ? "Backend not running." : "No model configured yet."
      color: root.backendUnreachable ? Color.urgent : (root.bar ? root.bar.foreground : Color.foreground)
      font.bold: true
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    Text {
      visible: root.backendUnreachable && root.setupResult !== "ok"
      text: "Builds the backend, generates a token/TLS cert, and starts it as a"
        + " systemd service (no custom listen port here - run"
        + " ./scripts/setup.sh yourself in a terminal for that)."
      color: root.bar ? root.bar.foreground : Color.foreground
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    Column {
      visible: root.registryIsDefault && root.skillLinkResult === ""
      width: parent.width
      spacing: Style.space(2)

      Text {
        text: "Also point a coding agent at skills/modelmanager/SKILL.md so it"
          + " can set up your first model - check which one(s) before clicking"
          + " below (or leave both unchecked and edit "
          + root.registryJsonPath + " yourself):"
        color: root.bar ? root.bar.foreground : Color.foreground
        wrapMode: Text.Wrap
        width: parent.width
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
      Row {
        spacing: Style.space(16)
        LabeledToggle {
          checked: root.linkClaudeChecked
          label: "Claude Code"
          foreground: root.bar ? root.bar.foreground : Color.foreground
          onToggled: root.linkClaudeChecked = !root.linkClaudeChecked
        }
        LabeledToggle {
          checked: root.linkCodexChecked
          label: "Codex"
          foreground: root.bar ? root.bar.foreground : Color.foreground
          onToggled: root.linkCodexChecked = !root.linkCodexChecked
        }
      }
    }

    Column {
      visible: root.registryIsDefault && root.ollamaRegisterResult === ""
      width: parent.width
      spacing: Style.space(2)

      Text {
        text: "Already running Ollama with a model pulled? Register one for a"
          + " quick first test - doesn't download anything, just points at"
          + " whichever model Ollama already has:"
        color: root.bar ? root.bar.foreground : Color.foreground
        wrapMode: Text.Wrap
        width: parent.width
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
      Row {
        spacing: Style.space(8)
        LabeledToggle {
          checked: root.registerOllamaChecked
          label: "Register an existing Ollama model"
          foreground: root.bar ? root.bar.foreground : Color.foreground
          onToggled: root.registerOllamaChecked = !root.registerOllamaChecked
        }
      }
    }

    Row {
      spacing: Style.space(8)

      Item {
        id: setupButtonFrame
        width: setupButton.implicitWidth
        height: setupButton.implicitHeight
        // Duplicates setupButton's own `visible` expression rather than
        // binding to setupButton.visible directly - forwarding a child's
        // `visible` into an ancestor's `visible` binding silently breaks
        // in this QML engine (the ancestor never becomes visible, no
        // warning logged) even though reading any other child property
        // the same way works fine. Found by bisecting this exact binding.
        visible: root.setupResult !== "ok" || (root.registryIsDefault
          && (root.skillLinkResult === "" || root.ollamaRegisterResult === ""))

        // Pulsing glow pulls the eye to the one button that matters on a
        // fresh install - only while it's actually the "get started" CTA
        // (backend down, nothing running yet). Same oscillating-opacity
        // technique as the tailscale panel's own account-switch indicator,
        // just a plain Rectangle instead of a real blur since nothing else
        // in this plugin pulls in a graphical-effects dependency. Kept
        // subtle on purpose - just enough to catch the eye, not a strobe.
        Rectangle {
          anchors.centerIn: parent
          width: parent.width + Style.space(10)
          height: parent.height + Style.space(10)
          radius: Style.cornerRadius + Style.space(2)
          color: Color.accent
          opacity: 0.0
          visible: root.backendUnreachable && !root.setupRunning

          SequentialAnimation on opacity {
            running: parent.visible
            loops: Animation.Infinite
            NumberAnimation { to: 0.22; duration: 1100; easing.type: Easing.InOutQuad }
            NumberAnimation { to: 0.06; duration: 1100; easing.type: Easing.InOutQuad }
          }
        }

        Button {
          id: setupButton
          anchors.fill: parent
          text: root.setupRunning ? "Running setup..." : root.skillLinkRunning ? "Linking..."
            : root.ollamaRegisterRunning ? "Registering..."
            : (root.backendUnreachable ? "Run setup now" : "Apply")
          enabled: !root.setupRunning && !root.skillLinkRunning && !root.ollamaRegisterRunning
          bordered: true
          foreground: root.backendUnreachable ? Color.accent : (root.bar ? root.bar.foreground : Color.foreground)
          visible: root.setupResult !== "ok" || (root.registryIsDefault
            && (root.skillLinkResult === "" || root.ollamaRegisterResult === ""))
          onClicked: root.startSetupAction()
        }
      }
    }

    Text {
      visible: root.setupRunning && root.setupLiveOutput !== ""
      text: root.setupLiveOutput
      color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
    Text {
      visible: root.setupResult !== "" && root.setupResult !== "ok"
      text: root.setupResult
      color: Color.urgent
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
    Text {
      visible: root.skillLinkResult !== "" && root.skillLinkResult !== "skipped"
      text: root.skillLinkResult
      color: root.skillLinkResult.indexOf("failed") !== -1
        ? Color.urgent : (root.bar ? root.bar.foreground : Color.foreground)
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
    Text {
      visible: root.ollamaRegisterResult !== "" && root.ollamaRegisterResult !== "skipped"
      text: root.ollamaRegisterResult
      color: (root.ollamaRegisterResult.indexOf("failed") !== -1 || root.ollamaRegisterResult.indexOf("No Ollama") !== -1)
        ? Color.urgent : (root.bar ? root.bar.foreground : Color.foreground)
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    Text {
      visible: root.backendUnreachable
      text: "(" + root.lastError + ")"
      color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  Text {
    visible: root.lastError !== "" && !root.backendUnreachable && root.activeHost === ""
    text: root.lastError
    color: Color.urgent
    wrapMode: Text.Wrap
    width: parent.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }

  // Remote-target errors get their own always-shown line (unlike the
  // local-only cases above, which route through the first-run setup
  // block or the plain lastError text above) - a remote peer being
  // unreachable or refusing a request (e.g. a grant missing that
  // system) is never "run setup.sh", so none of that gating applies.
  Text {
    visible: root.activeHost !== "" && root.lastError !== ""
    text: "Peer '" + root.activeHost + "': " + root.lastError
    color: Color.urgent
    wrapMode: Text.Wrap
    width: parent.width
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }

  // Hidden entirely while a host trust decision is pending (see
  // hostTrustBlock above) - a Column positioner skips invisible
  // children when sizing itself, so this shrinks to just whatever
  // error/trust text is showing instead of leaving this stale behind it.
  Column {
    visible: !root.hostTrustPromptActive
    width: parent.width
    spacing: Style.space(10)

    PanelSeparator {}
    PanelSectionHeader { text: "Stats" }

    Flickable {
      id: statsFlick
      width: parent.width
      height: Math.min(Style.space(240), contentHeight)
      contentWidth: width
      contentHeight: statsColumn.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      flickableDirection: Flickable.VerticalFlick
      interactive: contentHeight > height
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      // Same wheel trap as modelsFlick (components/ModelsList.qml) - see
      // its comment.
      WheelHandler {
        target: null
        onWheel: (event) => {
          var maxY = Math.max(0, statsFlick.contentHeight - statsFlick.height)
          statsFlick.contentY = Math.max(0, Math.min(maxY, statsFlick.contentY - event.angleDelta.y))
          event.accepted = true
        }
      }

      Column {
        id: statsColumn
        width: statsFlick.width
        spacing: Style.space(10)

        Repeater {
          model: root.filteredSystemNames
          delegate: Column {
            width: statsColumn.width
            spacing: Style.space(4)

            Text {
              text: modelData + (
                root.activeModelsBySystem[modelData] && root.activeModelsBySystem[modelData].length > 0
                  ? "  (" + root.activeModelsBySystem[modelData].join(", ") + ")"
                  : ""
              )
              color: root.bar ? root.bar.foreground : Color.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Repeater {
              model: (root.systemStats[modelData] && root.systemStats[modelData].gpus) || []
              delegate: Column {
                width: parent.width
                spacing: Style.space(2)

                Text {
                  text: (modelData.name || "GPU") + " (#" + modelData.index + ")"
                  color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.1)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                  width: parent.width
                }

                Rectangle {
                  width: parent.width
                  height: Style.space(6)
                  radius: height / 2
                  color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 2.2)

                  Rectangle {
                    width: parent.width * root.vramPct(modelData)
                    height: parent.height
                    radius: height / 2
                    color: Color.accent
                  }
                }

                Text {
                  text: "VRAM " + modelData.memory_used_mb + " / " + modelData.memory_total_mb + " MB ("
                    + Math.round(root.vramPct(modelData) * 100) + "%)   Util " + modelData.utilization_pct
                    + "%   " + modelData.temperature_c + "°C"
                  color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                  width: parent.width
                }
              }
            }

            Text {
              visible: !(root.systemStats[modelData] && root.systemStats[modelData].gpus)
              text: (root.systemStats[modelData] && root.systemStats[modelData].error) || "loading..."
              color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
              width: parent.width
            }
          }
        }
      }
    }
  }
}
