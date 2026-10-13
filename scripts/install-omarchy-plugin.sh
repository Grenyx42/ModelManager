#!/bin/bash
# Installs (or re-syncs) the ModelManager Omarchy bar-widget plugin and makes
# sure it's showing in the bar. Same discovery shape as any other Omarchy
# shell plugin: a folder under ~/.config/omarchy/plugins/, scanned by
# omarchy-shell.
#
# The plugin's manifest.json lives at this repo's root (required for
# marketplace submission), so this repo doubles as the plugin "folder" - but
# `omarchy plugin validate` rejects any symlink anywhere inside that folder,
# and a dev checkout's .venv always contains some (lib64 -> lib, etc). So
# instead of symlinking the live repo straight in (which also means every
# dev-only file - .git, .venv, registry.json - would sit inside the plugin
# folder too), this rsyncs just the plugin-relevant files across. Re-run
# after editing Panel.qml to pick up the change (hot-reload-on-symlink
# wasn't reliable here anyway - see the "Editing Panel.qml" note in the
# README).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$REPO_DIR/manifest.json")"
PLUGIN_DST="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

# Omarchy never runs install hooks for a plugin (by design - see its docs),
# so there's no other point in the install flow where a missing runtime venv
# would get caught; Panel.qml just fails silently if modelmanager isn't there.
if [[ ! -x "$HOME/.local/share/modelmanager/venv/bin/modelmanager" ]]; then
  echo "warning: ~/.local/share/modelmanager/venv not found - the bar widget" >&2
  echo "  will enable but won't show any models until you run:" >&2
  echo "    $REPO_DIR/scripts/setup.sh" >&2
fi

if [[ -L "$PLUGIN_DST" ]]; then
  echo "Removing old symlink-based install at $PLUGIN_DST..."
  rm "$PLUGIN_DST"
fi

mkdir -p "$PLUGIN_DST"
echo "Syncing plugin files to $PLUGIN_DST..."
rsync -a --delete \
  --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.pytest_cache' --exclude='*.egg-info' --exclude='external' \
  --exclude='registry.json' \
  "$REPO_DIR/" "$PLUGIN_DST/"

echo "Validating plugin manifest..."
omarchy plugin validate "$PLUGIN_DST"

echo "Reloading omarchy-shell's plugin registry..."
omarchy-shell shell rescanPlugins >/dev/null

echo "Enabling $PLUGIN_ID..."
omarchy plugin enable "$PLUGIN_ID" || true  # already-enabled isn't an error

echo "--- current state ---"
omarchy plugin list | grep -E "^ID|$PLUGIN_ID" || true
