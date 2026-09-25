#!/bin/bash
# Installs (or re-syncs) the ModelManager Omarchy bar-widget plugin and makes
# sure it's showing in the bar. Same install shape as any other Omarchy shell
# plugin (e.g. stappmus.dell-power-profiles): a symlinked/copied folder under
# ~/.config/omarchy/plugins/, discovered and hot-reloaded by omarchy-shell.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_SRC="$REPO_DIR/omarchy/plugin"
PLUGIN_ID="wade.modelmanager"
PLUGIN_LINK="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

if [[ ! -d "$PLUGIN_SRC" ]]; then
  echo "error: plugin source not found at $PLUGIN_SRC" >&2
  exit 1
fi

if [[ -L "$PLUGIN_LINK" ]]; then
  current_target="$(readlink -f "$PLUGIN_LINK")"
  if [[ "$current_target" != "$(readlink -f "$PLUGIN_SRC")" ]]; then
    echo "error: $PLUGIN_LINK is a symlink to something else ($current_target) - remove it by hand first" >&2
    exit 1
  fi
elif [[ -e "$PLUGIN_LINK" ]]; then
  echo "error: $PLUGIN_LINK already exists and isn't our symlink - remove/rename it by hand first" >&2
  exit 1
else
  mkdir -p "$(dirname "$PLUGIN_LINK")"
  ln -s "$PLUGIN_SRC" "$PLUGIN_LINK"
  echo "Linked $PLUGIN_LINK -> $PLUGIN_SRC"
fi

echo "Validating plugin manifest..."
omarchy plugin validate "$PLUGIN_SRC"

echo "Reloading omarchy-shell's plugin registry..."
omarchy-shell shell rescanPlugins >/dev/null

echo "Enabling $PLUGIN_ID..."
omarchy plugin enable "$PLUGIN_ID" || true  # already-enabled isn't an error

echo "--- current state ---"
omarchy plugin list | grep -E "^ID|$PLUGIN_ID" || true
