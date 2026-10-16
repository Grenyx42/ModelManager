import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Ui
import qs.Commons

// Bar icon + panel for the ModelManager coordinator. All state comes from
// shelling out to the `modelmanager` CLI, which just talks to the local
// coordinator's REST API (see modelmanager/cli.py) - the same pattern
// the first-party `agents` plugin uses for its usage collectors.
Panel {
  id: root
  moduleName: "slideout.modelmanager"
  ipcTarget: "slideout.modelmanager"

  // The bar host sizes our slot in the row from OUR implicitWidth/Height -
  // Item defaults those to 0, and BarIconButton's anchors.fill: parent then
  // has nothing to fill, so the icon exists but paints at zero size. Every
  // working bar-widget (e.g. dell-power-profiles) derives these from its
  // own button; we do the same.
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // omarchy-shell inherits Hyprland's session environment, not whatever PATH
  // an interactive terminal has after activating a venv - so the bare
  // command "modelmanager" isn't reliably found. scripts/setup.sh always
  // installs into this fixed location (not a venv next to this repo, since
  // this plugin might be installed anywhere - e.g. wherever `omarchy plugin
  // add` cloned it); set MODELMANAGER_BIN in your Hyprland env instead if
  // you've put it somewhere else on purpose.
  readonly property string mmBin: {
    const override = Quickshell.env("MODELMANAGER_BIN") || ""
    return override.length > 0 ? override : Quickshell.env("HOME") + "/.local/share/modelmanager/venv/bin/modelmanager"
  }

  // A brand-new install (setup.sh never run) has no mmBin at all yet -
  // Quickshell's Process never fires onExited for a genuinely missing
  // binary (confirmed directly: just a "Process failed to start" warning
  // on quickshell's own stderr, no QML signal at all), so every onExited
  // handler below would silently never run and the panel would just sit
  // empty forever with no error, no fresh-install guidance, nothing.
  // Routing through a real shell (which always exists and always starts)
  // turns a missing mmBin into an ordinary nonzero exit (127) with a
  // stderr message, which onExited already knows how to handle.
  function mmCommand(args) {
    return ["sh", "-c", "exec \"$0\" \"$@\"", root.mmBin].concat(args)
  }

  // Resolved relative to this file's own install location - works unchanged
  // whether this is a dev checkout (repo root) or a marketplace install
  // (install-omarchy-plugin.sh's rsync includes registry.clean.json, same
  // as every other plugin-relevant file). The "file://" -> plain-path strip
  // is the same idiom the shell's own notifications/clipboard code uses.
  readonly property string registryCleanPath: String(Qt.resolvedUrl("registry.clean.json")).substring(7)

  // Mirrors modelmanager/config.py's default_registry_path() - same
  // MODELMANAGER_REGISTRY override, same XDG_CONFIG_HOME fallback - so this
  // always points at the same file the coordinator itself would read.
  readonly property string registryJsonPath: {
    const override = Quickshell.env("MODELMANAGER_REGISTRY") || ""
    if (override.length > 0) return override
    const xdgConfig = Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config")
    return xdgConfig + "/modelmanager/registry.json"
  }

  // True once we've confirmed the live registry.json is missing entirely, or
  // byte-identical to the shipped empty starter (registry.clean.json) -
  // i.e. nobody has described their own machines/models yet. Only
  // meaningful alongside backendUnreachable below; set by
  // registryDefaultCheckProc, see checkFreshInstall().
  property bool registryIsDefault: false

  // Both of lastError's two "the coordinator itself isn't reachable" shapes -
  // see listProc.onExited (mmBin missing/not executable) and parseModels's
  // data.error branch (mmBin runs fine, but can't reach the REST service).
  // Anything else in lastError (a bad response, raw stderr) is a different
  // kind of problem and shouldn't trigger first-time-setup guidance.
  readonly property bool backendUnreachable: root.lastError.indexOf("cannot reach coordinator") !== -1
    || root.lastError.indexOf("can't run " + root.mmBin) !== -1

  // Resolved the same way as registryCleanPath above - lets the "Run setup
  // now" button below drive scripts/setup.sh directly from wherever this
  // plugin is actually installed, instead of just telling the user to go
  // find a terminal. Run with no TTY attached, setup.sh already detects
  // that itself and skips its own interactive prompts (listen-address
  // customization, agent-skill linking) - this button exists specifically
  // to cover everything setup.sh does unattended; the skill-link buttons
  // below cover the one thing it can't do headless.
  readonly property string setupShPath: String(Qt.resolvedUrl("scripts/setup.sh")).substring(7)
  readonly property string skillDirPath: String(Qt.resolvedUrl("skills/modelmanager")).substring(7)

  property bool setupRunning: false
  // "" (not run yet) | "ok" | an error string to show in red.
  property string setupResult: ""

  property bool skillLinkRunning: false
  // "" (not chosen yet) | "linked" | "skipped" | an error string.
  property string skillLinkResult: ""

  function runSetup() {
    if (setupProc.running) return
    root.setupRunning = true
    root.setupResult = ""
    setupProc.command = ["sh", "-c", "exec \"$0\"", root.setupShPath]
    setupProc.running = true
  }

  function linkSkill(agent) {
    if (skillLinkProc.running) return
    root.skillLinkRunning = true
    root.skillLinkResult = ""
    const dst = Quickshell.env("HOME") + "/." + agent + "/skills/modelmanager"
    skillLinkProc.command = [
      "sh", "-c", "mkdir -p \"$(dirname \"$2\")\" && ln -sf \"$1\" \"$2\"",
      "sh", root.skillDirPath, dst
    ]
    skillLinkProc.running = true
  }

  property var models: []
  property var groupedModels: []
  // registry.json's top-level "groups": named model sets ("start my usual
  // set"), as [{name, models: [...]}] sorted by name. Nothing to do with the
  // gpu_group headers inside the Models list - those are physical GPU
  // contention sets that happen to share the word "group".
  property var groups: []
  property var systemNames: []
  // gpu_group name -> system name, from `modelmanager config`'s top-level
  // "gpu_groups" (each one names the system it physically lives on - see
  // GpuGroupConfig in config.py). A model's own "gpu_groups" list (every
  // candidate's gpu_group, deduped) is what it's *configured* to be able to
  // run on - independent of whether it's active right now - so this is what
  // filteredSystemNames maps through, not the active candidate's host.
  property var gpuGroupSystem: ({})
  // Lives on root, not inside the popup's content tree, so it survives
  // closing and reopening the panel.
  property string modelFilter: ""
  property int runningCount: 0
  property string lastError: ""
  // Result of the most recent Ensure/Stop button click, if it failed - see
  // parseActionResult(). Cleared at the start of the next ensure()/stopModel()
  // call, so it only ever shows the latest action's outcome.
  property string actionError: ""

  // Per-system GPU stats, keyed by system name - {gpus: [...]} on success,
  // {error: "..."} on failure, absent entirely while the first sweep hasn't
  // reached that system yet. Always reassigned wholesale (never mutated in
  // place) so the Repeater below actually re-evaluates its bindings.
  property var systemStats: ({})
  property var statsQueue: []
  property bool statsSweepActive: false

  // Default sweep cadence is slow (30s) since nothing's changing; a
  // start/stop action bumps it to 1s for fastStatsWindowMs so the GPU bars
  // visibly react to whatever you just did, then it settles back down.
  readonly property int slowStatsIntervalMs: 30000
  readonly property int fastStatsIntervalMs: 1000
  readonly property int fastStatsWindowMs: 30000
  property bool fastStatsMode: false

  // Tracks reload_count from GET /v1/config/reload-status so a toast can
  // fire the moment the coordinator actually reloads registry.json - whether
  // that reload was the automatic RegistryWatcher noticing a hand edit, or a
  // manual `modelmanager reload`. Starts at -1 (unknown) so the very first
  // poll just establishes a baseline instead of firing a toast on plugin
  // startup.
  property int lastReloadCount: -1
  property bool toastVisible: false
  property string toastText: ""

  // Set from GET /v1/config/reload-status's last_reload_error - non-empty
  // means the most recent reload attempt (auto or manual) failed validation
  // (e.g. a hand edit left registry.json invalid) and every attempt since
  // has kept failing the same way. Drives the blinking bar icon below.
  property string registryError: ""
  readonly property bool hasError: root.registryError !== "" || root.lastError !== ""
  property bool blinkPhase: false

  readonly property int refreshIntervalSec: {
    var n = parseInt(String(setting("refreshIntervalSec", 15)), 10)
    return isFinite(n) && n > 0 ? n : 15
  }

  readonly property var filteredGroupedModels: root.filterGroupedModels(root.groupedModels, root.modelFilter, root.gpuGroupSystem)
  readonly property var filteredGroups: root.filterGroups(root.groups, root.modelFilter, root.gpuGroupSystem)

  // Systems the current filter is "about": the ones the filtered models are
  // configured to run on, plus the ones behind any filtered group's members.
  // The group half matters because a filter can match a group and no model at
  // all - "dev" hits "pi-dev" but no model name - and the group's machines
  // still have to show their GPU stats below.
  readonly property var filteredSystemNameSet: {
    var matched = root.systemNamesForModels(root.filteredGroupedModels, root.gpuGroupSystem)
    var fromGroups = root.systemNamesForGroups(root.filteredGroups, root.gpuGroupSystem)
    for (var sys in fromGroups) matched[sys] = true
    return matched
  }

  // Unfiltered shows every known system, same as before; filtered narrows it
  // down to only the systems the filter is about (via their gpu_groups),
  // whether or not anything is active on them right now.
  readonly property var filteredSystemNames: root.modelFilter.trim() === ""
    ? root.systemNames
    : root.systemNames.filter(function(n) { return root.filteredSystemNameSet[n] })

  // system name -> list of currently-active model names actually loaded on
  // it (unfiltered - the Stats header always reflects live reality, even
  // while the Models filter narrows what's listed above), so each Stats
  // entry can show what's warm and using its VRAM right now.
  readonly property var activeModelsBySystem: root.activeModelNamesBySystem(root.models, root.gpuGroupSystem)

  function refresh() {
    if (!listProc.running) listProc.running = true
    if (!systemsProc.running) systemsProc.running = true
    if (!gpuGroupsProc.running) gpuGroupsProc.running = true
    if (!groupsProc.running) groupsProc.running = true
  }

  function checkReloadStatus() {
    if (!reloadStatusProc.running) reloadStatusProc.running = true
  }

  // Kicked off the moment the coordinator looks unreachable, to tell "never
  // set up" apart from "configured, but the service happens to be down" -
  // only the former should show first-time-setup guidance instead of the
  // raw error. "test ! -e || cmp -s" treats a missing live file the same as
  // one that's an untouched copy of the empty starter (both mean "nobody's
  // described their machines yet"); only an existing file that actually
  // differs counts as "already configured".
  function checkFreshInstall() {
    if (registryDefaultCheckProc.running) return
    registryDefaultCheckProc.command = [
      "sh", "-c", "test ! -e \"$1\" || cmp -s \"$1\" \"$2\"",
      "sh", root.registryJsonPath, root.registryCleanPath
    ]
    registryDefaultCheckProc.running = true
  }

  function parseReloadStatus(text) {
    try {
      var data = JSON.parse(text)
      // cli.py's clean {"error": "..."} for an unreachable coordinator -
      // surface it the same way parseModels does, but don't touch
      // lastReloadCount/registryError since we don't actually know their
      // state without a working connection.
      if (data && typeof data === "object" && data.error && typeof data.reload_count !== "number") {
        root.lastError = data.error
        return
      }
      var count = data.reload_count
      if (typeof count === "number") {
        if (root.lastReloadCount >= 0 && count > root.lastReloadCount) {
          root.showToast("Model definitions refreshed")
        }
        root.lastReloadCount = count
      }
      root.registryError = data.last_reload_error || ""
    } catch (e) {
      // malformed response - leave state as-is and just try again next tick
    }
  }

  function showToast(text) {
    root.toastText = text
    root.toastVisible = true
    toastTimer.restart()
  }

  // ensure/stop both used to just discard their output and refresh() - a
  // failure (most commonly an SSH problem reaching a remote system: host
  // down, agent.ps1 deploy rejected, etc. - see errors.py's
  // HostUnreachableError/CommandFailedError) silently did nothing visible
  // at all, leaving someone clicking "Ensure" with no idea why the model
  // never came up. cli.py always prints the coordinator's {"error","code"}
  // body (or its own clean "cannot reach coordinator..." line) as JSON to
  // stdout regardless of success/failure, so this is the one parse this
  // covers every case.
  function parseActionResult(action, name, text) {
    try {
      var data = JSON.parse(text)
      if (data && typeof data === "object" && data.error) {
        root.actionError = action + " " + name + ": " + data.error
        return
      }
    } catch (e) {
      if (text && text.trim() !== "") {
        root.actionError = action + " " + name + ": " + text.trim()
        return
      }
    }
    root.actionError = ""
  }

  function ensure(name) {
    if (ensureProc.running) return
    root.actionError = ""
    ensureProc.pendingName = name
    ensureProc.command = root.mmCommand(["ensure", name])
    ensureProc.running = true
  }

  function stopModel(name) {
    if (stopProc.running) return
    root.actionError = ""
    stopProc.pendingName = name
    stopProc.command = root.mmCommand(["stop", name])
    stopProc.running = true
  }

  function parseGroups(text) {
    try {
      var data = JSON.parse(text)
      if (!data || typeof data !== "object" || data.error) return
      var names = []
      for (var k in data) names.push(k)
      names.sort()
      var out = []
      for (var i = 0; i < names.length; i++) out.push({ name: names[i], models: data[names[i]] || [] })
      groups = out
    } catch (e) {
      // leave groups as-is; listProc already surfaces a dead coordinator
    }
  }

  function modelByName(name) {
    for (var i = 0; i < root.models.length; i++) {
      if (root.models[i].name === name) return root.models[i]
    }
    return null
  }

  // {total, active} for a group - how many of its members are up right now.
  // Recomputed from root.models, which parseModels always reassigns wholesale,
  // so this re-evaluates on every refresh.
  function groupState(group) {
    var members = group.models || []
    var active = 0
    for (var i = 0; i < members.length; i++) {
      var m = root.modelByName(members[i])
      if (m && m.active) active++
    }
    return { total: members.length, active: active }
  }

  function groupStatusColor(st) {
    if (st.total > 0 && st.active === st.total) return Color.accent
    if (st.active > 0) return Qt.darker(Color.accent, 1.5)
    return Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.6)
  }

  // Which machine a model lives on, for the "sleek:decision-4b" member
  // labels. A model's gpu_groups are what it's *configured* to run on (every
  // candidate's gpu_group, deduped), so a model with candidates on two boxes
  // genuinely belongs to both - joined with "/" rather than silently picking
  // one. Empty when the config sweep hasn't landed yet, in which case the
  // label falls back to the bare model name.
  function systemForModel(name) {
    var m = root.modelByName(name)
    if (!m) return ""
    var gpuGroups = m.gpu_groups || []
    var systems = []
    for (var i = 0; i < gpuGroups.length; i++) {
      var sys = root.gpuGroupSystem[gpuGroups[i]]
      if (sys && systems.indexOf(sys) === -1) systems.push(sys)
    }
    return systems.join("/")
  }

  function memberLabel(name) {
    var sys = root.systemForModel(name)
    return sys === "" ? name : sys + ":" + name
  }

  // Groups share the Models list and so have to share its filter too, or
  // filtering to one model would leave unrelated groups stranded above it.
  // Matches on the group's own name as well as its members' - typing a model
  // name should surface the groups that contain it.
  function filterGroups(list, filterText, gpuGroupSystemMap) {
    var needle = filterText.trim().toLowerCase()
    if (needle === "") return list
    var result = []
    for (var i = 0; i < list.length; i++) {
      var g = list[i]
      var hit = g.name.toLowerCase().indexOf(needle) !== -1
      for (var j = 0; !hit && j < (g.models || []).length; j++) {
        var memberName = g.models[j]
        var member = root.modelByName(memberName)
        hit = memberName.toLowerCase().indexOf(needle) !== -1
          || (member !== null && root.modelMatchesFilter(member, needle, gpuGroupSystemMap))
      }
      if (hit) result.push(g)
    }
    return result
  }

  function memberStatusColor(name) {
    var m = root.modelByName(name)
    if (m && m.active) return Color.accent
    return Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.6)
  }

  function ensureGroup(name) {
    if (groupEnsureProc.running) return
    root.actionError = ""
    groupEnsureProc.pendingName = name
    groupEnsureProc.command = root.mmCommand(["group-ensure", name])
    groupEnsureProc.running = true
  }

  function stopGroup(name) {
    if (groupStopProc.running) return
    root.actionError = ""
    groupStopProc.pendingName = name
    groupStopProc.command = root.mmCommand(["group-stop", name])
    groupStopProc.running = true
  }

  // group-ensure/group-stop are deliberately best-effort per member (see
  // Manager.ensure_group): the whole call reports 200 with a per-model
  // results list even when some members failed, so "did it work" can't be
  // read off the exit code the way parseActionResult does for a single model.
  // Name the members that failed, since "2 of 3 up" alone doesn't say which.
  function parseGroupActionResult(action, name, text) {
    try {
      var data = JSON.parse(text)
      if (data && data.error) {
        root.actionError = action + " " + name + ": " + data.error
        return
      }
      if (data && data.results) {
        var failed = []
        for (var i = 0; i < data.results.length; i++) {
          var r = data.results[i]
          if (!r.ok) failed.push(r.name + " (" + (r.detail || "failed") + ")")
        }
        root.actionError = failed.length === 0
          ? ""
          : action + " " + name + ": " + failed.length + " of " + data.results.length + " failed - " + failed.join("; ")
        return
      }
    } catch (e) {
      if (text && text.trim() !== "") {
        root.actionError = action + " " + name + ": " + text.trim()
        return
      }
    }
    root.actionError = ""
  }

  // Opens a real terminal live-tailing this model's actual log file. Looks up
  // where that file is and how to reach it (`log-path`) first, since that
  // differs by engine/OS: a plain local path, or an SSH host to tail (or
  // Get-Content -Wait, on a Windows box - it has no tail(1)) it on. This is
  // the one place the plugin talks SSH directly instead of only the
  // coordinator's REST API, since the whole point is a real streaming shell,
  // which the REST layer can't give it.
  property string pendingTerminalModel: ""

  function openLogTerminal(name) {
    if (logPathProc.running) return
    pendingTerminalModel = name
    logPathProc.command = root.mmCommand(["log-path", name])
    logPathProc.running = true
  }

  function launchTerminalForLogPath(rawText) {
    var name = root.pendingTerminalModel
    var followCmd = null
    try {
      var info = JSON.parse(rawText)
      var logPaths = info.paths || []
      // Usually one file, but a remote Windows launch keeps stdout/stderr as
      // two separate files (see agent.ps1) - and the process being launched
      // almost always logs to stderr, so tailing only the first one here
      // would look like the terminal just isn't showing anything.
      if (logPaths.length > 0) {
        if (info.control === "ssh" && info.os === "windows") {
          var psPaths = logPaths.map(function(p) {
            return "'" + p.replace(/'/g, "''") + "'"
          }).join(",")
          followCmd = [
            "ssh", info.ssh_host, "powershell", "-NoProfile", "-Command",
            "Get-Content -Path " + psPaths + " -Wait -Tail 100"
          ]
        } else if (info.control === "ssh") {
          followCmd = ["ssh", info.ssh_host, "tail", "-f", "-n", "100"].concat(logPaths)
        } else {
          followCmd = ["tail", "-f", "-n", "100"].concat(logPaths)
        }
      }
    } catch (e) {
      // fall through to the polling fallback below
    }
    if (!followCmd) {
      // No discrete log file to tail (e.g. an externally-managed ollama
      // service, only in journalctl) - fall back to polling the same
      // CLI/REST path the popup itself uses.
      followCmd = ["watch", "-n", "2", root.mmBin, "logs", name, "--lines", "100"]
    }
    // xdg-terminal-exec's two-token "--title <value>" form silently mis-parses
    // (swallows the value as if it were the command), even with no spaces in
    // it - only the "--title=<value>" single-token form works reliably here.
    terminalProc.command = ["xdg-terminal-exec", "--title=ModelManager logs: " + name, "--"].concat(followCmd)
    terminalProc.running = false
    terminalProc.running = true
  }

  // Sweeps every system's stats one at a time (statsSweepProc only runs one
  // command at a time, and a home-lab's system count is small enough that
  // sequential polling is simpler than juggling a Process per system).
  function startStatsSweep() {
    if (root.statsSweepActive) return
    root.statsQueue = root.systemNames.slice()
    root.statsSweepActive = true
    root.pollNextSystemStats()
  }

  function pollNextSystemStats() {
    if (root.statsQueue.length === 0) {
      root.statsSweepActive = false
      return
    }
    var sys = root.statsQueue.shift()
    statsSweepProc.pendingSystem = sys
    statsSweepProc.command = root.mmCommand(["stats", sys])
    statsSweepProc.running = true
  }

  // Bumps polling to fastStatsIntervalMs for fastStatsWindowMs and kicks an
  // immediate sweep, instead of waiting up to slowStatsIntervalMs for the
  // next scheduled one - called right after an ensure/stop actually changes
  // something on a system.
  function triggerFastStats() {
    root.fastStatsMode = true
    fastStatsWindowTimer.restart()
    root.startStatsSweep()
  }

  function parseSystemStats(system, text) {
    // Empty stdout means the CLI wrote to stderr instead - let the stderr
    // handler report that, rather than clobbering it with a blank entry.
    if (text.trim() === "") return
    var entry = { gpus: null, error: "" }
    try {
      var data = JSON.parse(text)
      if (data.gpus) {
        entry.gpus = data.gpus
      } else {
        entry.error = data.error || text
      }
    } catch (e) {
      entry.error = text
    }
    var copy = {}
    for (var key in root.systemStats) copy[key] = root.systemStats[key]
    copy[system] = entry
    root.systemStats = copy
  }

  function setSystemStatsError(system, text) {
    var copy = {}
    for (var key in root.systemStats) copy[key] = root.systemStats[key]
    copy[system] = { gpus: null, error: text }
    root.systemStats = copy
  }

  function vramPct(gpu) {
    if (!gpu.memory_total_mb) return 0
    return Math.max(0, Math.min(1, gpu.memory_used_mb / gpu.memory_total_mb))
  }

  function groupModels(list) {
    var byGroup = {}
    var order = []
    for (var i = 0; i < list.length; i++) {
      var m = list[i]
      var g = (m.gpu_groups && m.gpu_groups.length > 0) ? m.gpu_groups.join(" + ") : "(no gpu_group)"
      if (!byGroup[g]) { byGroup[g] = []; order.push(g) }
      byGroup[g].push(m)
    }
    order.sort()
    var grouped = []
    for (var j = 0; j < order.length; j++) grouped.push({ group: order[j], models: byGroup[order[j]] })
    return grouped
  }

  function parseModels(text) {
    try {
      var data = JSON.parse(text)
      // cli.py prints a clean {"error": "..."} line (instead of a Python
      // traceback) when the coordinator itself is unreachable - that parses
      // fine as JSON but isn't a model list, so it needs its own check
      // before being treated as one.
      if (data && typeof data === "object" && !Array.isArray(data) && data.error) {
        lastError = data.error
        root.checkFreshInstall()
        return
      }
      models = data
      groupedModels = groupModels(data)
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
      systemNames = Object.keys(JSON.parse(text))
    } catch (e) {
      // leave systemNames as-is; systems list is secondary to model status
    }
  }

  // `modelmanager config` dumps the whole registry; only its top-level
  // "gpu_groups" is needed here; see gpuGroupSystem's own comment above.
  function parseGpuGroupSystems(text) {
    try {
      var data = JSON.parse(text)
      var map = {}
      var groups = data.gpu_groups || {}
      for (var name in groups) map[name] = groups[name].system
      gpuGroupSystem = map
    } catch (e) {
      // leave gpuGroupSystem as-is
    }
  }

  // Keeps only the models (within each gpu_group) whose name matches
  // modelFilter (case-insensitive substring); a group with no matches drops
  // out entirely rather than showing an empty header.
  // True if the model's own name matches, or - via its gpu_groups - any
  // system it's configured to run on does. Typing a server name ("coder")
  // surfaces every model that could land there, the same way typing part of
  // a model's own name does; needle is already trim()med/lowercased by the
  // caller.
  function modelMatchesFilter(model, needle, gpuGroupSystemMap) {
    if (model.name.toLowerCase().indexOf(needle) !== -1) return true
    var groups = model.gpu_groups || []
    for (var i = 0; i < groups.length; i++) {
      var sys = gpuGroupSystemMap[groups[i]]
      if (sys && sys.toLowerCase().indexOf(needle) !== -1) return true
    }
    return false
  }

  function filterGroupedModels(grouped, filterText, gpuGroupSystemMap) {
    var needle = filterText.trim().toLowerCase()
    if (needle === "") return grouped
    var result = []
    for (var i = 0; i < grouped.length; i++) {
      var matched = grouped[i].models.filter(function(m) {
        return root.modelMatchesFilter(m, needle, gpuGroupSystemMap)
      })
      if (matched.length > 0) result.push({ group: grouped[i].group, models: matched })
    }
    return result
  }

  // Which systems the given (already filtered) models are configured to run
  // on - via every candidate's gpu_group, deduped onto "gpu_groups" in the
  // model listing - regardless of whether any of them are active right now.
  function systemNamesForModels(grouped, gpuGroupSystemMap) {
    var matched = {}
    for (var g = 0; g < grouped.length; g++) {
      var models = grouped[g].models
      for (var m = 0; m < models.length; m++) {
        var groups = models[m].gpu_groups || []
        for (var i = 0; i < groups.length; i++) {
          var sys = gpuGroupSystemMap[groups[i]]
          if (sys) matched[sys] = true
        }
      }
    }
    return matched
  }

  // Systems behind a set of groups, reached through their member models.
  // Mirrors systemNamesForModels, but a group only holds member *names*, so
  // each one has to be looked up in root.models first; a name with no model
  // (a group member deleted from the registry) just contributes nothing.
  function systemNamesForGroups(groups, gpuGroupSystemMap) {
    var matched = {}
    for (var g = 0; g < groups.length; g++) {
      var members = groups[g].models || []
      for (var m = 0; m < members.length; m++) {
        var model = root.modelByName(members[m])
        if (!model) continue
        var gpuGroups = model.gpu_groups || []
        for (var i = 0; i < gpuGroups.length; i++) {
          var sys = gpuGroupSystemMap[gpuGroups[i]]
          if (sys) matched[sys] = true
        }
      }
    }
    return matched
  }

  // Maps each active model to the system it's actually loaded on right now,
  // via its active candidate's own gpu_group (not gpu_groups - the model
  // could have candidates on several systems, but this one's specifically
  // where it landed) - precise even though candidate.host may not equal the
  // system's own key name.
  function activeModelNamesBySystem(models, gpuGroupSystemMap) {
    var bySystem = {}
    for (var i = 0; i < models.length; i++) {
      var model = models[i]
      if (!model.active || !model.candidate) continue
      var sys = gpuGroupSystemMap[model.candidate.gpu_group]
      if (!sys) continue
      if (!bySystem[sys]) bySystem[sys] = []
      bySystem[sys].push(model.name)
    }
    return bySystem
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

  // Runs regardless of refreshIntervalSec and whether the panel is open, at
  // a fixed, short interval - the whole point is to notice a reload (and pop
  // the toast) quickly while someone's actively hand-editing registry.json,
  // not to wait out a 15s+ user-configured refresh.
  Timer {
    interval: 3000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.checkReloadStatus()
  }

  Timer {
    id: toastTimer
    interval: 2500
    onTriggered: root.toastVisible = false
  }

  // Drives the bar icon's blink while an error is present (registry.json
  // failed to reload, or the coordinator's unreachable). Only ticks while
  // there's actually something to show, and root.hasError's own binding
  // resolves the icon back to fully opaque the instant it clears - no
  // separate "stop and reset opacity" bookkeeping needed.
  Timer {
    interval: 500
    running: root.hasError
    repeat: true
    onTriggered: root.blinkPhase = !root.blinkPhase
  }

  // Drives the always-visible per-system Stats sweep. Only runs while the
  // popup is open (triggeredOnStart means reopening it refreshes right
  // away); interval itself switches between slow/fast based on fastStatsMode,
  // set by triggerFastStats() after an ensure/stop action.
  Timer {
    interval: root.fastStatsMode ? root.fastStatsIntervalMs : root.slowStatsIntervalMs
    running: root.opened
    repeat: true
    triggeredOnStart: true
    onTriggered: root.startStatsSweep()
  }

  Timer {
    id: fastStatsWindowTimer
    interval: root.fastStatsWindowMs
    onTriggered: root.fastStatsMode = false
  }

  Process {
    id: listProc
    command: root.mmCommand(["list"])
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
    // A missing/unreadable mmBin (venv never set up - see scripts/setup.sh)
    // now reaches here as a normal nonzero exit via mmCommand()'s sh
    // wrapper (see its own comment) - sh's own "command not found"/"not
    // executable" exit codes (127/126) always mean mmBin itself is the
    // problem, so they take priority and overwrite lastError even if sh's
    // raw stderr ("No such file or directory") already landed there first;
    // without this, the panel would silently sit empty forever with no
    // indication why.
    onExited: (code) => {
      if (code === 126 || code === 127 || (code !== 0 && root.lastError === "")) {
        root.lastError = "can't run " + root.mmBin + " - run this repo's scripts/setup.sh, or set MODELMANAGER_BIN if installed elsewhere"
        root.checkFreshInstall()
      }
    }
  }

  Process {
    id: registryDefaultCheckProc
    command: []
    onExited: (code) => { root.registryIsDefault = (code === 0) }
  }

  Process {
    id: setupProc
    stdout: StdioCollector { id: setupOut; waitForEnd: true }
    stderr: StdioCollector {
      id: setupErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.setupResult = text.trim() }
    }
    onExited: (code) => {
      root.setupRunning = false
      if (code === 0) {
        // Overrides any benign stderr noise above - a zero exit from
        // setup.sh's own `set -e` means it actually finished successfully.
        root.setupResult = "ok"
        root.refresh()
        root.checkFreshInstall()
      } else if (root.setupResult === "") {
        root.setupResult = "setup.sh failed (exit " + code + ")"
      }
    }
  }

  Process {
    id: skillLinkProc
    stderr: StdioCollector {
      id: skillLinkErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.skillLinkResult = text.trim() }
    }
    onExited: (code) => {
      root.skillLinkRunning = false
      if (code === 0 && root.skillLinkResult === "") root.skillLinkResult = "linked"
      else if (code !== 0 && root.skillLinkResult === "") root.skillLinkResult = "link failed (exit " + code + ")"
    }
  }

  Process {
    id: reloadStatusProc
    command: root.mmCommand(["reload-status"])
    stdout: StdioCollector { id: reloadStatusOut; waitForEnd: true; onStreamFinished: root.parseReloadStatus(text) }
  }

  Process {
    id: systemsProc
    command: root.mmCommand(["systems"])
    stdout: StdioCollector {
      id: systemsOut
      waitForEnd: true
      onStreamFinished: root.parseSystems(text)
    }
  }

  Process {
    id: gpuGroupsProc
    command: root.mmCommand(["config"])
    stdout: StdioCollector {
      id: gpuGroupsOut
      waitForEnd: true
      onStreamFinished: root.parseGpuGroupSystems(text)
    }
  }

  Process {
    id: ensureProc
    property string pendingName: ""
    command: []
    stdout: StdioCollector {
      id: ensureOut
      waitForEnd: true
      onStreamFinished: root.parseActionResult("ensure", ensureProc.pendingName, text)
    }
    onExited: (code) => {
      if (code !== 0 && root.actionError === "") {
        root.actionError = "ensure " + ensureProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }

  Process {
    id: stopProc
    property string pendingName: ""
    command: []
    stdout: StdioCollector {
      id: stopOut
      waitForEnd: true
      onStreamFinished: root.parseActionResult("stop", stopProc.pendingName, text)
    }
    onExited: (code) => {
      if (code !== 0 && root.actionError === "") {
        root.actionError = "stop " + stopProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }

  Process {
    id: groupsProc
    command: root.mmCommand(["groups"])
    stdout: StdioCollector {
      id: groupsOut
      waitForEnd: true
      onStreamFinished: root.parseGroups(text)
    }
  }

  Process {
    id: groupEnsureProc
    property string pendingName: ""
    command: []
    stdout: StdioCollector {
      id: groupEnsureOut
      waitForEnd: true
      onStreamFinished: root.parseGroupActionResult("group-ensure", groupEnsureProc.pendingName, text)
    }
    onExited: (code) => {
      if (code !== 0 && root.actionError === "") {
        root.actionError = "group-ensure " + groupEnsureProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }

  Process {
    id: groupStopProc
    property string pendingName: ""
    command: []
    stdout: StdioCollector {
      id: groupStopOut
      waitForEnd: true
      onStreamFinished: root.parseGroupActionResult("group-stop", groupStopProc.pendingName, text)
    }
    onExited: (code) => {
      if (code !== 0 && root.actionError === "") {
        root.actionError = "group-stop " + groupStopProc.pendingName + " failed (exit " + code + ")"
      }
      root.refresh()
      root.triggerFastStats()
    }
  }

  Process {
    id: statsSweepProc
    property string pendingSystem: ""
    command: []
    stdout: StdioCollector {
      id: statsSweepOut
      waitForEnd: true
      onStreamFinished: root.parseSystemStats(statsSweepProc.pendingSystem, text)
    }
    stderr: StdioCollector {
      id: statsSweepErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.setSystemStatsError(statsSweepProc.pendingSystem, text.trim()) }
    }
    onExited: root.pollNextSystemStats()
  }

  Process {
    id: logPathProc
    command: []
    stdout: StdioCollector { id: logPathOut; waitForEnd: true; onStreamFinished: root.launchTerminalForLogPath(text) }
    stderr: StdioCollector {
      id: logPathErr
      waitForEnd: true
      onStreamFinished: { if (text.trim() !== "") root.launchTerminalForLogPath("") }
    }
  }

  // Fire-and-forget: the terminal window it opens runs independently of us,
  // so there's nothing to collect here.
  Process {
    id: terminalProc
    command: []
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // oct-cpu, JetBrainsMono Nerd Font - the brain emoji it replaced read as an
    // indistinct blob at bar size; this chip glyph stays legible that small.
    text: ""
    slotSize: Style.bar.iconSlot
    tooltipText: root.registryError !== "" ? root.registryError
      : (root.backendUnreachable ? "ModelManager: backend not running - click for details"
      : (root.lastError !== "" ? root.lastError : (root.runningCount + " model(s) running")))
    opacity: root.hasError && root.blinkPhase ? 0.25 : 1.0
    Behavior on opacity { NumberAnimation { duration: 150 } }
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    // KeyboardPanel's own onOpenChanged defers this through Qt.callLater and
    // the Wayland/Hyprland focus-priming dance (see its own comments) - that
    // already-tuned timing is what actually lands the cursor in the filter
    // field reliably on open (hotkey or bar click, both just flip
    // root.opened the same way), not something to duplicate here.
    focusTarget: modelFilterField
    contentWidth: panel.fittedContentWidth(Style.space(420))
    // Fixed height rather than fitted-to-content: the popup is always the
    // same size whatever's in it, and each section scrolls inside its own
    // slot (see the layout comment below). cappedContentHeight still clamps
    // to the space actually available beside the bar on a short screen.
    contentHeight: panel.cappedContentHeight(Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Keys.priority: BeforeItem means this would otherwise see every
      // keystroke before the focused filter field does (h/j/k/l would fire
      // moveRequested mid-typing, etc.) - the documented pattern for an
      // inline text editor inside a PanelKeyCatcher (see its own comments)
      // is exactly this: short-circuit while the editor has focus.
      blocked: modelFilterField.activeFocus
      onCloseRequested: root.close()

      // Pinned above the model list (not inside it) so the Models header/
      // filter stays reachable no matter how far that list is scrolled -
      // otherwise scrolling down hides the one field you'd want to clear or
      // change the filter from.
      Item {
        id: modelsHeaderRow
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        implicitHeight: Math.max(modelsHeader.implicitHeight, modelFilterField.implicitHeight)
        height: implicitHeight

        PanelSectionHeader {
          id: modelsHeader
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: "Models"
        }

        TextField {
          id: modelFilterField
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(160)
          verticalPadding: Style.space(2)
          placeholderText: "filter models..."
          foreground: root.bar ? root.bar.foreground : Color.foreground
          text: root.modelFilter
          onTextChanged: root.modelFilter = text
          // keyCatcher is blocked while this has focus (see its own
          // comment), which also swallows its Escape-to-close handling -
          // restore that expected behavior directly here instead.
          Keys.onEscapePressed: root.close()
        }
      }

      // Fixed-height popup: the card is always Style.space(560) tall (see
      // contentHeight above), and nothing scrolls as a whole - there's no
      // outer Flickable any more. The two lists each scroll inside their own
      // slot instead: Stats sits pinned to the bottom at its natural height
      // (capped, so a box with many GPUs scrolls rather than eating the
      // popup), and Models takes whatever is left between the pinned header
      // and it. contentHolder in KeyboardPanel.qml doesn't clip its children,
      // so both slots clip themselves.
      Flickable {
        id: modelsFlick
        anchors.top: modelsHeaderRow.bottom
        anchors.topMargin: Style.space(10)
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: bottomBlock.top
        anchors.bottomMargin: Style.space(10)
        contentWidth: width
        contentHeight: modelsColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

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
              readonly property bool starting: groupEnsureProc.running && groupEnsureProc.pendingName === modelData.name
              readonly property bool stopping: groupStopProc.running && groupStopProc.pendingName === modelData.name

              Row {
                width: parent.width
                spacing: Style.space(8)

                Rectangle {
                  width: Style.space(8)
                  height: Style.space(8)
                  radius: width / 2
                  anchors.verticalCenter: parent.verticalCenter
                  color: root.groupStatusColor(groupEntry.st)
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width - groupActionBtn.implicitWidth - Style.space(24)
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
                  text: groupEntry.starting ? "Starting..." : (groupEntry.stopping ? "Stopping..." : (groupEntry.allUp ? "Stop" : "Ensure"))
                  enabled: !groupEnsureProc.running && !groupStopProc.running
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
                      readonly property bool starting: ensureProc.running && ensureProc.pendingName === modelData.name
                      text: starting ? "Starting..." : (modelData.active ? "Stop" : "Ensure")
                      // ensure() drops a second call while one is in flight,
                      // so every other inactive model's button is a dead
                      // click too until this one finishes - telling the same
                      // lie we're fixing here. Stop stays live throughout:
                      // it runs on its own process, independent of this one.
                      enabled: !(ensureProc.running && !modelData.active)
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

      // Everything that lives below the model list, pinned to the bottom of
      // the card so Models can fill the gap above it. Messages come first so
      // an error never gets pushed off-card by a tall Stats section.
      Column {
        id: bottomBlock
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
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

        // Shown whenever the backend itself isn't reachable. Independent of
        // the registry-setup block below (not the same && condition the
        // whole thing used to be gated on) because clicking "Run setup now"
        // flips this to false the moment the coordinator comes up, but the
        // registry stays the untouched clean default until a real model is
        // added - the skill-link offer below needs to survive that, not
        // disappear the instant the backend starts.
        Column {
          visible: root.backendUnreachable
          width: parent.width
          spacing: Style.space(4)

          Text {
            text: "Backend not running."
            color: Color.urgent
            font.bold: true
            wrapMode: Text.Wrap
            width: parent.width
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          Row {
            visible: root.setupResult !== "ok"
            spacing: Style.space(8)

            Button {
              text: root.setupRunning ? "Running setup..." : "Run setup now"
              enabled: !root.setupRunning
              bordered: true
              onClicked: root.runSetup()
            }
          }
          Text {
            visible: root.setupResult !== "ok"
            text: "Builds the backend, generates a token/TLS cert, and starts it as a"
              + " systemd service (no custom listen port here - run"
              + " ./scripts/setup.sh yourself in a terminal for that)."
            color: root.bar ? root.bar.foreground : Color.foreground
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
            text: "(" + root.lastError + ")"
            color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.3)
            wrapMode: Text.Wrap
            width: parent.width
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }

        // Shown as long as the registry is still the untouched clean
        // default - i.e. nobody has described a real model yet - regardless
        // of whether the backend above is up. Offering to link the skill
        // asks every time this situation is seen (no persisted dismissal),
        // same one-time-per-situation spirit as scripts/setup.sh's own
        // prompt; "Skip" just hides it for this session.
        Column {
          visible: root.registryIsDefault && root.skillLinkResult === ""
          width: parent.width
          spacing: Style.space(4)

          Text {
            text: (root.backendUnreachable ? "2. " : "No model configured yet - ")
              + "point your coding agent at skills/modelmanager/SKILL.md to set"
              + " one up, or edit " + root.registryJsonPath + " yourself."
            color: root.bar ? root.bar.foreground : Color.foreground
            wrapMode: Text.Wrap
            width: parent.width
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          Row {
            spacing: Style.space(8)

            Button {
              text: root.skillLinkRunning ? "Linking..." : "Link: Claude Code"
              enabled: !root.skillLinkRunning
              bordered: true
              onClicked: root.linkSkill("claude")
            }
            Button {
              text: root.skillLinkRunning ? "Linking..." : "Link: Codex"
              enabled: !root.skillLinkRunning
              bordered: true
              onClicked: root.linkSkill("codex")
            }
            Button {
              text: "Skip"
              bordered: true
              onClicked: root.skillLinkResult = "skipped"
            }
          }
        }
        Text {
          visible: root.skillLinkResult === "linked"
          text: "Linked - your agent can see the skill now."
          color: root.bar ? root.bar.foreground : Color.foreground
          wrapMode: Text.Wrap
          width: parent.width
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
        Text {
          visible: root.skillLinkResult !== "" && root.skillLinkResult !== "skipped" && root.skillLinkResult !== "linked"
          text: root.skillLinkResult
          color: Color.urgent
          wrapMode: Text.Wrap
          width: parent.width
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }

        Text {
          visible: root.lastError !== "" && !root.backendUnreachable
          text: root.lastError
          color: Color.urgent
          wrapMode: Text.Wrap
          width: parent.width
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }

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

          // Same wheel trap as modelsFlick above - see its comment.
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
  }

  // Small self-dismissing toast confirming a registry.json reload actually
  // happened, independent of whether the panel above is open - this is the
  // whole point, since you're watching the *file*, not necessarily the bar
  // popup, while hand-editing it. A dedicated layer-shell surface (like
  // omarchy-shell's own OSD) rather than routing through that shared OSD
  // singleton, so this plugin doesn't have to reach into shell-owned state
  // for something purely its own.
  PanelWindow {
    id: toastWindow
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
}
