#!/bin/bash
# Undoes scripts/setup.sh. Removes what setup created; leaves what is yours.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PARENT="$HOME/.local/share/modelmanager"
UNIT="$HOME/.config/systemd/user/modelmanager.service"
REGISTRY="$HOME/.config/modelmanager/registry.json"
TOKEN="$HOME/.config/modelmanager/token"
TLS_DIR="$HOME/.config/modelmanager/tls"
ENV_FILE="$HOME/.config/modelmanager/env"
GRANTS="$HOME/.config/modelmanager/grants.json"
BIN_LINK="$HOME/.local/bin/modelmanager"
PLUGIN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$REPO_DIR/manifest.json" 2>/dev/null || echo "")"
SKILL_SRC="$REPO_DIR/skills/modelmanager"
OPENCODE_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
SKILL_DSTS=(
  "$HOME/.claude/skills/modelmanager"
  "$HOME/.codex/skills/modelmanager"
  "$OPENCODE_CONFIG_DIR/skills/modelmanager"
  "$HOME/.cursor/skills/modelmanager"
  "$HOME/.hermes/skills/modelmanager"
)

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
[[ -f "$ENV_FILE" ]] && note "$ENV_FILE - coordinator listen host/port"
[[ -f "$GRANTS" ]] && note "$GRANTS - multi-user access grants"
note "Delete them by hand if you're sure you're done with this."

# These point into a *different* tool's personal config (same reasoning
# setup.sh gives before creating them) - report them and ask before
# touching them too, rather than leaving dangling symlinks unmentioned.
LINKED_SKILLS=()
for dst in "${SKILL_DSTS[@]}"; do
  if [[ -L "$dst" ]] && [[ "$(readlink -f "$dst" 2>/dev/null)" == "$(readlink -f "$SKILL_SRC" 2>/dev/null)" ]]; then
    LINKED_SKILLS+=("$dst")
  fi
done
if [[ ${#LINKED_SKILLS[@]} -gt 0 ]]; then
  printf '\n\033[1mAgent skill links:\033[0m\n'
  for dst in "${LINKED_SKILLS[@]}"; do
    note "$dst"
  done
  if [[ -t 0 && -t 1 ]]; then
    read -r -p "Remove these skill links too? [y/N]: " REMOVE_SKILLS || REMOVE_SKILLS="n"
    if [[ "$REMOVE_SKILLS" =~ ^[Yy]$ ]]; then
      for dst in "${LINKED_SKILLS[@]}"; do
        rm -f "$dst"
        note "removed $dst"
      done
    else
      note "left in place - remove by hand if you want: rm ${LINKED_SKILLS[*]}"
    fi
  else
    note "not running interactively - remove by hand if you want: rm ${LINKED_SKILLS[*]}"
  fi
fi

if [[ -n "$PLUGIN_ID" ]]; then
  printf '\n\033[1mThe plugin itself:\033[0m\n'
  note "omarchy plugin remove $PLUGIN_ID"
fi
echo
