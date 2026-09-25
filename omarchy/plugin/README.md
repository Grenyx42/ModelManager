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

## Install (manual drop-in)

This plugin lives inside the ModelManager repo rather than its own git repo,
so install it as a symlink rather than `omarchy plugin add`:

```bash
ln -s "$(pwd)/omarchy/plugin" ~/.config/omarchy/plugins/wade.modelmanager
omarchy-shell shell rescanPlugins
omarchy plugin enable wade.modelmanager
omarchy bar move wade.modelmanager   # optional: pick where it sits
```

Editing `Panel.qml` afterwards hot-reloads automatically (the shell watches
everything under `~/.config/omarchy/plugins/`).

## Settings

`refreshIntervalSec` (default 15) controls how often the model list refreshes.
Per-system GPU stats and per-model logs are fetched on demand (clicking
Stats/Logs), not on the refresh timer, since a remote system's stats go over
SSH and shouldn't be polled continuously.

```bash
omarchy bar set wade.modelmanager refreshIntervalSec 30 --json
```
