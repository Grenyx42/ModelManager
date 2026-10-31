import QtQuick
import QtQuick.Controls
import Quickshell
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

  // Reached from Panel.qml's PanelKeyCatcher (see its `blocked` binding) so
  // typing in this field doesn't leak keystrokes into the panel's h/j/k/l
  // shortcuts, same as modelFilterField/hostField already get.
  property alias statsFilterFieldFocus: statsFilterField.activeFocus

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
  // A plain Flickable, not a bordered box like the status/logs/key-files
  // boxes nested inside it - this wraps the *whole* setup/diagnosis
  // guidance block. Its content varies a lot by state (plain setup text
  // vs. diagnosis-mode status/logs boxes vs. a mid-install live-output
  // box, all potentially stacking up at once), and a plain Column here
  // just kept growing past the popup's own fixed contentHeight, printing
  // its later lines off the bottom edge of the card entirely instead of
  // behind a scrollbar. Math.min only kicks in once content would
  // actually overflow - the common short case still sizes to fit exactly
  // as before.
  Flickable {
    id: setupGuidanceFlick
    visible: root.firstRunSetupActive
    width: parent.width
    // Fills the card (bottomBlock's own height now stretches to match -
    // see Panel.qml) while the backend's actually down, rather than
    // shrinking to content and leaving the rest of the card empty below
    // it. Once reachable again this Flickable isn't even visible, so the
    // Math.min/content-fit behavior there is moot either way.
    height: root.localBackendDown ? parent.height : Math.min(Style.space(420), contentHeight)
    contentWidth: width
    contentHeight: setupGuidanceColumn.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    flickableDirection: Flickable.VerticalFlick
    interactive: contentHeight > height
    // Plain AsNeeded, not the always-visible PanelScrollBar the nested
    // status/logs/live-output boxes use below - this outer Flickable is a
    // rare-case overflow safety net, not a primary scroll surface, and an
    // always-on track running down the whole guidance block read as "this
    // entire area scrolls" when really only those inner log boxes
    // normally do. Stays invisible unless content genuinely overflows.
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    WheelHandler {
      target: null
      onWheel: (event) => {
        var maxY = Math.max(0, setupGuidanceFlick.contentHeight - setupGuidanceFlick.height)
        setupGuidanceFlick.contentY = Math.max(0, Math.min(maxY, setupGuidanceFlick.contentY - event.angleDelta.y))
        event.accepted = true
      }
    }

    Column {
    id: setupGuidanceColumn
    width: setupGuidanceFlick.width
    spacing: Style.space(6)

    Text {
      text: root.backendUnreachable ? "ModelManager back-end isn't running." : "No model configured yet."
      color: root.backendUnreachable ? Color.urgent : (root.bar ? root.bar.foreground : Color.foreground)
      font.bold: true
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: root.backendUnreachable ? Style.font.bodySmall : Style.font.caption
    }

    // This paragraph, the skill-link/Ollama-register choices below, and
    // the Key files box all answer "what am I about to do / what do I
    // need to decide first" - once setupRunning, those choices are
    // already locked in (applied automatically after setup.sh succeeds -
    // see startSetupAction()/applyPendingSkillLinks()) and none of it is
    // actionable any more, so it just crowds out the one thing actually
    // worth looking at while it runs: the live log below. Hiding all of
    // it specifically while setupRunning (each via `&& !root.setupRunning`
    // on its own existing `visible`) frees up enough room that the log
    // usually doesn't need to scroll at all for a normal, fast install.
    Text {
      visible: root.backendUnreachable && root.setupResult !== "ok" && !root.diagnosisModeActive && !root.setupRunning
      text: "Click setup below to build the backend environment, generate its security token and TLS cert, and then start it as a"
        + " systemd service."
      color: root.bar ? root.bar.foreground : Color.foreground
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    // Basics only - a token file existing means setup.sh ran here before,
    // so a connection-refused error is something to diagnose (crashed,
    // manually stopped, box just rebooted) rather than something to set
    // up. The button below still runs the same idempotent setup.sh either
    // way (see root.diagnosisModeActive's own comment) - re-running it is
    // a legitimate fix (its last step is `systemctl --user enable --now`),
    // just mislabeled as "setup" when nothing actually needs configuring.
    Text {
      visible: root.diagnosisModeActive && root.setupResult !== "ok"
      text: "A previous setup was found on this machine, so the service should already be configured -"
        + " it just isn't running right now. Here's its current status:"
      color: root.bar ? root.bar.foreground : Color.foreground
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    // Basics only (see this Column's own comment above) - raw command
    // output in a bordered, scrollable box (same border treatment as the
    // "Key files / info" box below, same height-capped Flickable pattern
    // as the Stats section's statsFlick, just without its Repeater since
    // there's only ever one block of text here).
    Text {
      visible: root.diagnosisModeActive && root.setupResult !== "ok"
      text: "Service status:"
      color: root.bar ? root.bar.foreground : Color.foreground
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    Rectangle {
      visible: root.diagnosisModeActive && root.setupResult !== "ok"
      x: Style.space(10)
      width: parent.width - Style.space(10)
      height: Style.space(110)
      radius: Style.cornerRadius
      color: "transparent"
      border.width: 1
      border.color: Qt.rgba((root.bar ? root.bar.foreground : Color.foreground).r,
        (root.bar ? root.bar.foreground : Color.foreground).g,
        (root.bar ? root.bar.foreground : Color.foreground).b, 0.25)

      Flickable {
        id: diagnosisStatusFlick
        anchors.fill: parent
        anchors.margins: Style.space(6)
        contentWidth: width
        contentHeight: diagnosisStatusText.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: PanelScrollBar { handleColor: root.bar ? root.bar.foreground : Color.foreground }

        WheelHandler {
          target: null
          onWheel: (event) => {
            var maxY = Math.max(0, diagnosisStatusFlick.contentHeight - diagnosisStatusFlick.height)
            diagnosisStatusFlick.contentY = Math.max(0, Math.min(maxY, diagnosisStatusFlick.contentY - event.angleDelta.y))
            event.accepted = true
          }
        }

        Text {
          id: diagnosisStatusText
          text: root.diagnosisStatusOutput !== "" ? root.diagnosisStatusOutput : "Checking..."
          color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
          wrapMode: Text.WrapAnywhere
          width: diagnosisStatusFlick.width
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
    }

    Text {
      visible: root.diagnosisModeActive && root.setupResult !== "ok"
      text: "Recent logs:"
      color: root.bar ? root.bar.foreground : Color.foreground
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    Rectangle {
      visible: root.diagnosisModeActive && root.setupResult !== "ok"
      x: Style.space(10)
      width: parent.width - Style.space(10)
      height: Style.space(140)
      radius: Style.cornerRadius
      color: "transparent"
      border.width: 1
      border.color: Qt.rgba((root.bar ? root.bar.foreground : Color.foreground).r,
        (root.bar ? root.bar.foreground : Color.foreground).g,
        (root.bar ? root.bar.foreground : Color.foreground).b, 0.25)

      Flickable {
        id: diagnosisLogsFlick
        anchors.fill: parent
        anchors.margins: Style.space(6)
        contentWidth: width
        contentHeight: diagnosisLogsText.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: PanelScrollBar { handleColor: root.bar ? root.bar.foreground : Color.foreground }

        WheelHandler {
          target: null
          onWheel: (event) => {
            var maxY = Math.max(0, diagnosisLogsFlick.contentHeight - diagnosisLogsFlick.height)
            diagnosisLogsFlick.contentY = Math.max(0, Math.min(maxY, diagnosisLogsFlick.contentY - event.angleDelta.y))
            event.accepted = true
          }
        }

        Text {
          id: diagnosisLogsText
          text: root.diagnosisLogsOutput !== "" ? root.diagnosisLogsOutput : "Checking..."
          color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
          wrapMode: Text.WrapAnywhere
          width: diagnosisLogsFlick.width
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
    }

    Column {
      visible: root.registryIsDefault && root.skillLinkResult === "" && !root.diagnosisModeActive && !root.setupRunning
      width: parent.width
      spacing: Style.space(2)

      Text {
        text: "Optionally, select one or more coding agents to link the ModelManager SKILL.md into."
        color: root.bar ? root.bar.foreground : Color.foreground
        wrapMode: Text.Wrap
        width: parent.width
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
      Column {
        spacing: Style.space(6)
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
        Row {
          spacing: Style.space(16)
          LabeledToggle {
            checked: root.linkOpenCodeChecked
            label: "OpenCode"
            foreground: root.bar ? root.bar.foreground : Color.foreground
            onToggled: root.linkOpenCodeChecked = !root.linkOpenCodeChecked
          }
          LabeledToggle {
            checked: root.linkCursorChecked
            label: "CursorCLI"
            foreground: root.bar ? root.bar.foreground : Color.foreground
            onToggled: root.linkCursorChecked = !root.linkCursorChecked
          }
        }
        Row {
          spacing: Style.space(16)
          LabeledToggle {
            checked: root.linkHermesChecked
            label: "Hermes"
            foreground: root.bar ? root.bar.foreground : Color.foreground
            onToggled: root.linkHermesChecked = !root.linkHermesChecked
          }
        }
      }
    }

    Item {
      visible: root.registryIsDefault && root.skillLinkResult === "" && !root.diagnosisModeActive && !root.setupRunning
      width: 1
      height: Style.space(3)
    }

    Column {
      visible: root.registryIsDefault && root.ollamaRegisterResult === "" && !root.diagnosisModeActive && !root.setupRunning
      width: parent.width
      spacing: Style.space(2)

      Text {
        text: "If you already have an Ollama model running locally, select below to find and register that model as a quick first test:"
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

    Item {
      visible: root.registryIsDefault && root.ollamaRegisterResult === "" && !root.diagnosisModeActive && !root.setupRunning
      width: 1
      height: Style.space(3)
    }

    Column {
      visible: root.backendUnreachable && root.setupResult !== "ok" && !root.setupRunning
      width: parent.width
      spacing: Style.space(2)

      Text {
        text: "Key files / info:"
        color: root.bar ? root.bar.foreground : Color.foreground
        wrapMode: Text.Wrap
        width: parent.width
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      Rectangle {
        x: Style.space(10)
        width: parent.width - Style.space(10)
        height: fileLocationsText.implicitHeight + Style.space(12)
        radius: Style.cornerRadius
        color: "transparent"
        border.width: 1
        border.color: Qt.rgba((root.bar ? root.bar.foreground : Color.foreground).r,
          (root.bar ? root.bar.foreground : Color.foreground).g,
          (root.bar ? root.bar.foreground : Color.foreground).b, 0.25)

        Text {
          id: fileLocationsText
          // 127.0.0.1 is always included alongside any detected Tailscale
          // IP (never replaced by it) - the CLI/bar widget below always
          // talk to 127.0.0.1, so dropping it would break local control.
          text: (root.detectedBindHost !== ""
              ? "Will bind to: " + (root.detectedBindViaTailscale
                  ? "127.0.0.1 and " + root.detectedBindHost + " (this machine, plus your tailnet via Tailscale)"
                  : "127.0.0.1 (this machine only - no active Tailscale detected)")
                + "\n"
              : "")
            + "Local CLI/plugin URL: " + root.localCoordinatorUrl + "\n"
            + root.skillDirPath + "/SKILL.md\n" + root.registryJsonPath
          color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
          wrapMode: Text.WrapAnywhere
          anchors.left: parent.left
          anchors.right: copyFileLocationsButton.left
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(8)
          anchors.rightMargin: Style.space(4)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }

        PanelActionButton {
          id: copyFileLocationsButton
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.rightMargin: Style.space(4)
          iconText: "󰆏"
          tooltipText: "Copy to clipboard"
          foreground: root.bar ? root.bar.foreground : Color.foreground
          onClicked: Quickshell.execDetached(["wl-copy", fileLocationsText.text])
        }
      }
    }

    Item {
      // Extra breathing room before the CTA - the Column's uniform
      // spacing between the explanatory paragraphs above reads fine
      // between text lines, but felt too cramped right before the button.
      width: 1
      height: Style.space(6)
    }

    Row {
      spacing: Style.space(8)

      Item {
        id: setupButtonFrame
        // Includes the glow's own halo margin (see below) rather than
        // letting the glow overflow a frame sized to just the button -
        // the guidance block now lives inside a clip:true Flickable (see
        // setupGuidanceFlick above, added to fix setup output overflowing
        // the popup), and an overflowing glow got asymmetrically clipped
        // by that Flickable's edges whenever the button sat flush against
        // one (commonly the left, since the Column has no left padding,
        // and the bottom, since the button is often the last tall thing
        // before the Flickable's own capped height) while the opposite
        // edges had room to spare - glow visible on top/right, missing on
        // bottom/left, exactly as reported. Sizing the frame to its own
        // full visual footprint instead keeps the glow entirely within
        // bounds the Flickable already accounts for.
        width: setupButton.implicitWidth + Style.space(10)
        height: setupButton.implicitHeight + Style.space(10)
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
          anchors.fill: parent
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
          anchors.centerIn: parent
          text: root.setupRunning ? (root.diagnosisModeActive ? "Restarting..." : "Running setup...") : root.skillLinkRunning ? "Linking..."
            : root.ollamaRegisterRunning ? "Registering..."
            : (root.diagnosisModeActive ? "Restart backend service" : (root.backendUnreachable ? "Run setup now" : "Apply"))
          enabled: !root.setupRunning && !root.skillLinkRunning && !root.ollamaRegisterRunning
          bordered: true
          foreground: root.backendUnreachable ? Color.accent : (root.bar ? root.bar.foreground : Color.foreground)
          visible: root.setupResult !== "ok" || (root.registryIsDefault
            && (root.skillLinkResult === "" || root.ollamaRegisterResult === ""))
          onClicked: root.startSetupAction()
        }
      }
    }

    // Once setup succeeds and both optional steps are resolved, the CTA
    // button above hides itself (see its own `visible`) - which used to
    // leave this whole block showing nothing but the "No model configured
    // yet." heading, with no indication of what to actually do next or
    // where the Coordinator/Models/Stats sections (still hidden - see
    // root.firstRunSetupActive) went. They come back on their own once
    // something's actually described (see parseModels's self-correcting
    // registryIsDefault re-check) - this just explains that in the
    // meantime instead of leaving a dead end.
    Text {
      visible: root.setupResult === "ok" && root.registryIsDefault
        && root.skillLinkResult !== "" && root.ollamaRegisterResult !== ""
      text: "Setup complete and the backend is running. Describe your first system and model - via a linked"
        + " coding agent or the modelmanager CLI - and the panel will automatically refresh once something's"
        + " configured."
      color: root.bar ? root.bar.foreground : Color.foreground
      wrapMode: Text.Wrap
      width: parent.width
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    // Bordered, scrollable box - same treatment as the diagnosis status/
    // logs boxes above. A plain wrapped Text here used to just keep
    // growing with setupLiveOutput (capped to 10 lines, but that can
    // still run taller than the popup's fixed contentHeight once stacked
    // under the setup paragraph/checkboxes/button), pushing its later
    // lines - the ones actually relevant mid-install - off the bottom
    // edge of the popup entirely rather than behind a scrollbar.
    Rectangle {
      id: setupLiveBox
      visible: root.setupRunning && root.setupLiveOutput !== ""
      width: parent.width
      // Grows to use whatever's left in the now-filled guidance area
      // (see setupGuidanceFlick/bottomBlock's own height comments) rather
      // than sitting at a small fixed height with empty card below it -
      // this is the box the user is actually watching mid-install, so
      // it's the one that should get the extra room. `y` is this Rectangle's
      // own Column-assigned position, read back to know how much space is
      // left below it; Style.space(110) stays as the floor for whenever
      // there isn't more to give (short content above it, or not filling
      // at all - e.g. the already-configured diagnosis "restart" path).
      height: root.localBackendDown
        ? Math.max(Style.space(110), setupGuidanceFlick.height - setupLiveBox.y - Style.space(10))
        : Style.space(110)
      radius: Style.cornerRadius
      color: "transparent"
      border.width: 1
      border.color: Qt.rgba((root.bar ? root.bar.foreground : Color.foreground).r,
        (root.bar ? root.bar.foreground : Color.foreground).g,
        (root.bar ? root.bar.foreground : Color.foreground).b, 0.25)

      Flickable {
        id: setupLiveFlick
        anchors.fill: parent
        anchors.margins: Style.space(6)
        contentWidth: width
        contentHeight: setupLiveText.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: PanelScrollBar { handleColor: root.bar ? root.bar.foreground : Color.foreground }

        WheelHandler {
          target: null
          onWheel: (event) => {
            var maxY = Math.max(0, setupLiveFlick.contentHeight - setupLiveFlick.height)
            setupLiveFlick.contentY = Math.max(0, Math.min(maxY, setupLiveFlick.contentY - event.angleDelta.y))
            event.accepted = true
          }
        }

        Text {
          id: setupLiveText
          text: root.setupLiveOutput
          color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
          wrapMode: Text.WrapAnywhere
          width: setupLiveFlick.width
          font.family: Style.font.family
          font.pixelSize: Style.font.caption

          // Stay pinned to the latest output while setup is running, same
          // as a terminal's own scrollback would - Qt.callLater so this
          // reads contentHeight after it's actually re-settled off the
          // text change, not the stale value from just before it.
          onTextChanged: {
            if (!root.setupRunning) return
            Qt.callLater(function() {
              setupLiveFlick.contentY = Math.max(0, setupLiveFlick.contentHeight - setupLiveFlick.height)
            })
          }
        }
      }
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
    visible: !root.hostTrustPromptActive && !root.firstRunSetupActive
    width: parent.width
    spacing: Style.space(10)

    PanelSeparator {}

    Item {
      id: statsHeaderRow
      width: parent.width
      implicitHeight: Math.max(statsHeader.implicitHeight, statsFilterField.implicitHeight)
      height: implicitHeight

      PanelSectionHeader {
        id: statsHeader
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        text: "Stats"
      }

      TextField {
        id: statsFilterField
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(160)
        verticalPadding: Style.space(2)
        placeholderText: "filter systems..."
        foreground: root.bar ? root.bar.foreground : Color.foreground
        text: root.statsFilter
        onTextChanged: root.statsFilter = text
        Keys.onEscapePressed: root.close()
      }
    }

    Flickable {
      id: statsFlick
      width: parent.width
      // Fixed slot (not Math.min-to-content) so this section takes a
      // steady, predictable share of the popup's fixed total height
      // instead of growing with however many systems/GPUs are reporting -
      // same fixed-slot-that-scrolls treatment as modelsFlick gets from
      // its own anchors in Panel.qml.
      height: Style.space(170)
      contentWidth: width
      contentHeight: statsColumn.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      flickableDirection: Flickable.VerticalFlick
      interactive: contentHeight > height
      ScrollBar.vertical: PanelScrollBar { handleColor: root.bar ? root.bar.foreground : Color.foreground }

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
        // Narrower than statsFlick itself (not just contentWidth) - the
        // VRAM bars and system-name/stat Text below all size off this via
        // `parent.width`, and PanelScrollBar's always-on track sits as an
        // overlay at the Flickable's own right edge regardless of content
        // width, so without this gap the usage bars were rendering right
        // underneath the scroll handle.
        width: statsFlick.width - Style.space(14)
        spacing: Style.space(10)

        Repeater {
          model: root.displayedSystemNames
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
