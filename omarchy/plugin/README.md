# ModelManager Omarchy plugin

Bar icon (● + running-model count) and click-through panel for the
ModelManager coordinator. Pure display + action layer: all state comes from
shelling out to the `modelmanager` CLI, which talks to the coordinator's
local REST API. No QML file talks to SSH, engines, or the registry directly.

## Requirements

- The `modelmanager` package installed so the `modelmanager` CLI is on
  `PATH` for the user running `omarchy-shell` (typically: `pip install -e .`
  from the repo root, or a pipx/venv install added to your shell's PATH).
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
Omarchy shell plugin (e.g. `stappmus.dell-power-profiles`). Same-day edits to
`Panel.qml` hot-reload automatically (the shell watches everything under
`~/.config/omarchy/plugins/`); re-run the script if the icon ever disappears
from the bar or after a manifest change.

`omarchy bar move wade.modelmanager` repositions it if you don't want it in
the default center section.

## Settings

`refreshIntervalSec` (default 15) controls how often the model list refreshes.
Per-system GPU stats and per-model logs are fetched on demand (clicking
Stats/Logs), not on the refresh timer, since a remote system's stats go over
SSH and shouldn't be polled continuously.

```bash
omarchy bar set wade.modelmanager refreshIntervalSec 30 --json
```
