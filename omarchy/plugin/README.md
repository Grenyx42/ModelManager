# ModelManager Omarchy plugin

Bar icon (🧠 + running-model count) and click-through panel for the
ModelManager coordinator. Pure display + action layer: all state comes from
shelling out to the `modelmanager` CLI, which talks to the coordinator's
local REST API. No QML file talks to SSH, engines, or the registry directly.

`Panel.qml` calls the CLI by its absolute venv path
(`$HOME/dev/ModelManager/.venv/bin/modelmanager`), not the bare command -
`omarchy-shell` runs with Hyprland's session environment from login, not an
interactive terminal's `PATH` after activating the repo's venv, so a bare
`"modelmanager"` in a `Process { command: [...] }` reliably fails to start.

## Requirements

- The venv set up per the top-level README (`python3 -m venv .venv && .venv/bin/pip install -e .`).
- The coordinator running (`modelmanager serve`, or the systemd user unit in
  `../../systemd/modelmanager.service`).

## Install / re-sync

This plugin lives inside the ModelManager repo rather than its own git repo,
so it's installed as a symlink rather than via `omarchy plugin add`:

```bash
../../scripts/install-omarchy-plugin.sh
```

That symlinks `omarchy/plugin` to `~/.config/omarchy/plugins/wade.modelmanager`
(idempotent - safe to re-run), validates the manifest, reloads
omarchy-shell's plugin registry, and enables it. Same shape as any other
Omarchy shell plugin (e.g. `stappmus.dell-power-profiles`). Default placement
lands it in the bar's center section; move it with:

```bash
omarchy bar move wade.modelmanager --section right
```

**Editing `Panel.qml`:** the "saving a file hot-reloads it" behavior turned
out to be unreliable in practice - a `sed -i` edit (which rewrites via a
temp file + rename) silently stopped the file-watcher from firing at all,
and even `omarchy-shell shell rescanPlugins` and a disable/enable cycle kept
running the previously-compiled version afterwards. If a change doesn't
seem to take effect within a few seconds of saving, don't trust
rescan/enable to fix it - run `omarchy-restart-shell` (kills and relaunches
the one long-running quickshell process; safe, and preserves layout/lock
state) and check `quickshell log -p /usr/share/omarchy/shell --tail 50` for
warnings/errors afterwards.

## Settings

`refreshIntervalSec` (default 15) controls how often the model list refreshes.
Per-system GPU stats and per-model logs are fetched on demand (clicking
Stats/Logs), not on the refresh timer, since a remote system's stats go over
SSH and shouldn't be polled continuously.

```bash
omarchy bar set wade.modelmanager refreshIntervalSec 30 --json
```
