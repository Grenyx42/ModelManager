#!/bin/bash
# Undoes scripts/setup.sh. Removes what setup created; leaves what is yours.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PARENT="$HOME/.local/share/modelmanager"
UNIT="$HOME/.config/systemd/user/modelmanager.service"
REGISTRY="$HOME/.config/modelmanager/registry.json"
TOKEN="$HOME/.config/modelmanager/token"
TLS_DIR="$HOME/.config/modelmanager/tls"
BIN_LINK="$HOME/.local/bin/modelmanager"
PLUGIN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$REPO_DIR/manifest.json" 2>/dev/null || echo "")"

note() { printf '  %s\n' "$*"; }

if [[ -f "$UNIT" ]]; then
  systemctl --user disable --now modelmanager.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  note "systemd service stopped and removed"
else
  note "no systemd service installed - nothing to stop"
fi

if [[ -d "$VENV_PARENT" ]]; then
  rm -rf "$VENV_PARENT"
  note "runtime venv removed ($VENV_PARENT)"
else
  note "no runtime venv found - nothing to remove"
fi

# Only remove it if it's still pointing into the venv we just deleted - never
# touch it if someone repointed it at something else on purpose.
if [[ -L "$BIN_LINK" ]] && [[ "$(readlink "$BIN_LINK")" == "$VENV_PARENT/venv/bin/modelmanager" ]]; then
  rm -f "$BIN_LINK"
  note "removed $BIN_LINK"
fi

printf '\n\033[1mLeft in place on purpose:\033[0m\n'
# Same reasoning as registry.json living outside the plugin/venv in the first
# place: someone running this to clean up a broken install, not to actually
# walk away, shouldn't lose the config describing their systems/engines/models.
[[ -f "$REGISTRY" ]] && note "$REGISTRY - your systems/engines/models config"
[[ -f "$TOKEN" ]] && note "$TOKEN - REST API bearer token"
[[ -d "$TLS_DIR" ]] && note "$TLS_DIR - self-signed TLS cert/key"
note "Delete them by hand if you're sure you're done with this."

if [[ -n "$PLUGIN_ID" ]]; then
  printf '\n\033[1mThe plugin itself:\033[0m\n'
  note "omarchy plugin remove $PLUGIN_ID"
fi
echo
