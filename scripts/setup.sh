#!/bin/bash
# One-shot bootstrap: builds the runtime venv and registry.json wherever this
# plugin is actually running from - run once, regardless of whether you
# cloned this repo yourself or installed it via `omarchy plugin add`, before
# enabling the Omarchy plugin or installing the systemd unit (both just
# invoke the venv below and don't set any of this up themselves).
#
# The venv lives at a fixed location outside this repo/install directory
# (~/.local/share/modelmanager/venv), not a `.venv` next to this script -
# an Omarchy-plugin-marketplace install can live anywhere (wherever
# `omarchy plugin add` cloned it) and can be replaced wholesale on an
# update, so the runtime venv needs to be independent of that. Same reasoning
# as the registry.json default (see modelmanager/config.py).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$HOME/.local/share/modelmanager/venv"
CONFIG_DIR="$HOME/.config/modelmanager"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  if ! command -v python3 >/dev/null; then
    echo "error: no python3 on PATH - install Python 3.11 or newer, then run this again" >&2
    exit 1
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "error: $(python3 -V 2>&1) found, but modelmanager needs Python 3.11 or newer" >&2
    exit 1
  fi
  echo "Creating venv at $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
fi

# Only what requirements.lock names, pinned and hash-verified - a loose
# `pip install -e .` would resolve whatever's newest on PyPI at install time,
# which isn't something you want for a service that runs on every login.
LOCK="$REPO_DIR/requirements.lock"
if [[ ! -f "$LOCK" ]]; then
  echo "error: missing $LOCK - refusing to install unpinned dependencies" >&2
  exit 1
fi
echo "Installing dependencies from requirements.lock (digests verified)..."
"$VENV_DIR/bin/pip" install -q --require-hashes -r "$LOCK"

# NOT an editable install (-e) - an editable install's import machinery
# points permanently back at $REPO_DIR itself, which on a marketplace
# install *is* ~/.config/omarchy/plugins/<id> - a directory Omarchy's shell
# watches recursively (inotifywait -m -r) and reloads the whole plugin on
# any change inside it. Every `modelmanager` invocation (and this plugin's
# bar widget invokes it constantly) writes/touches __pycache__ right there,
# which retriggers the watcher, which reloads the plugin, which invokes
# `modelmanager` again - confirmed directly causing a reload storm that
# stops the panel from ever reaching a stable, rendered state. A plain
# install copies the package into the venv's own site-packages instead,
# so running it never touches the plugin's install directory again.
# --force-reinstall so re-running this script (e.g. after `omarchy plugin
# update`) always refreshes the copy even when pyproject.toml's version
# string hasn't changed between commits - an editable install didn't need
# this (it always read the live source), a plain one does. Also remove any
# editable-install leftover from a previous version of this script, so an
# existing install upgrading into this one actually stops hitting the
# reload storm instead of silently keeping the old editable pointer.
rm -f "$VENV_DIR"/lib/python*/site-packages/_editable_impl_modelmanager.pth
echo "Installing modelmanager itself (from $REPO_DIR, no deps - already satisfied above)..."
"$VENV_DIR/bin/pip" install -q --no-deps --force-reinstall "$REPO_DIR"

mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/registry.json" ]]; then
  # registry.clean.json (empty systems/gpu_groups/engines/models) rather than
  # registry.example.json - a fresh install should boot with nothing to
  # misconfigure, not someone else's 3-machine home lab. See
  # registry.example.json for the full annotated reference shape instead.
  cp "$REPO_DIR/registry.clean.json" "$CONFIG_DIR/registry.json"
  echo "Created $CONFIG_DIR/registry.json (empty) - point your coding agent at"
  echo "  skills/modelmanager/SKILL.md to set up your first model, or edit it"
  echo "  by hand (see registry.example.json for the full reference shape)."
else
  echo "$CONFIG_DIR/registry.json already exists - leaving it alone."
fi

# The coordinator refuses every REST request without this (see
# modelmanager/coordinator/app.py:install_auth_middleware) - generated once
# here so a fresh install never hits that. An existing install upgrading
# into this version won't have one yet; re-running this script (safe,
# idempotent) is the fix, or `modelmanager auth rotate`.
if [[ ! -f "$CONFIG_DIR/token" ]]; then
  TOKEN="$("$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
  ( umask 177 && printf '%s\n' "$TOKEN" > "$CONFIG_DIR/token" )
  chmod 600 "$CONFIG_DIR/token"
  echo "Generated a REST API bearer token at $CONFIG_DIR/token"
  echo "The CLI and bar plugin pick it up automatically (same user, same box)."
  echo "Copy it into any other app that calls the REST API directly: $TOKEN"
else
  echo "$CONFIG_DIR/token already exists - leaving it alone."
fi

# `modelmanager serve` refuses to start without these (see cmd_serve in
# cli.py) - a self-signed cert, since this never leaves your own machines
# and there's no public CA to ask. SAN covers localhost/127.0.0.1 (the CLI
# and bar plugin, same box) and this machine's own hostname (another box on
# your LAN/tailnet talking to it directly) - not a Tailscale MagicDNS name,
# which setup.sh has no reliable way to know in advance; regenerate by hand
# with your own -addext if you need one covered too.
TLS_DIR="$CONFIG_DIR/tls"
if [[ ! -f "$TLS_DIR/cert.pem" || ! -f "$TLS_DIR/key.pem" ]]; then
  if ! command -v openssl >/dev/null; then
    echo "error: openssl not found - required to generate the coordinator's TLS cert" >&2
    exit 1
  fi
  mkdir -p "$TLS_DIR"
  HOST_NAME="$(hostname -f 2>/dev/null || hostname)"
  openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
    -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
    -subj "/CN=modelmanager-coordinator" \
    -addext "subjectAltName=DNS:localhost,DNS:$HOST_NAME,IP:127.0.0.1" \
    >/dev/null 2>&1
  chmod 600 "$TLS_DIR/key.pem"
  chmod 644 "$TLS_DIR/cert.pem"
  echo "Generated a self-signed TLS cert at $TLS_DIR (covers localhost, 127.0.0.1, $HOST_NAME)"
  echo "Copy $TLS_DIR/cert.pem to any other box/app that calls the REST API directly, so it can verify this cert instead of disabling verification."
else
  echo "$TLS_DIR/cert.pem already exists - leaving it alone."
fi

# Optional listen host/port override, read as a systemd EnvironmentFile by
# the unit installed below (and picked up by a manual `modelmanager serve`
# too, via MODELMANAGER_HOST/MODELMANAGER_PORT - see cli.py). Written once,
# like the token/TLS cert above - skipped entirely when this file already
# exists (an existing customization is left alone) or when not running
# interactively.
ENV_FILE="$CONFIG_DIR/env"
if [[ -f "$ENV_FILE" ]]; then
  echo "$ENV_FILE already exists - leaving it alone."
elif [[ -t 0 && -t 1 ]]; then
  echo ""
  read -r -p "Customize the coordinator's listen address? (default 0.0.0.0:8778) [y/N]: " CUSTOMIZE_LISTEN || CUSTOMIZE_LISTEN="n"
  if [[ "$CUSTOMIZE_LISTEN" =~ ^[Yy]$ ]]; then
    read -r -p "  Host [0.0.0.0]: " LISTEN_HOST || LISTEN_HOST=""
    read -r -p "  Port [8778]: " LISTEN_PORT || LISTEN_PORT=""
    LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
    LISTEN_PORT="${LISTEN_PORT:-8778}"
    if ! [[ "$LISTEN_PORT" =~ ^[0-9]+$ ]]; then
      echo "warning: '$LISTEN_PORT' isn't a valid port - keeping the default 8778 instead" >&2
      LISTEN_PORT=8778
    fi
    {
      echo "MODELMANAGER_HOST=$LISTEN_HOST"
      echo "MODELMANAGER_PORT=$LISTEN_PORT"
    } > "$ENV_FILE"
    echo "Wrote $ENV_FILE - edit it any time, then: systemctl --user restart modelmanager"
    if [[ "$LISTEN_PORT" != "8778" ]]; then
      echo "This only changes what the systemd-managed coordinator binds to - the CLI"
      echo "  (and the Omarchy bar widget, which just shells out to it) still default to"
      echo "  port 8778 locally unless MODELMANAGER_PORT is also set wherever they run:"
      echo "  export MODELMANAGER_PORT=$LISTEN_PORT in your shell profile, and the same"
      echo "  in your Hyprland env (where MODELMANAGER_BIN would also go) for the bar widget."
    fi
  fi
fi

# Install + start the coordinator's own systemd --user unit - unlike the
# agent-skill link below (which edits a *different* tool's personal config
# and asks first), this is just making this project's own service run,
# the same category as the token/TLS cert above that are also created
# without asking. Skipped gracefully (not a fatal error) wherever a user
# systemd session isn't available (containers, some minimal/headless
# setups) - daemon-reload is the actual connectivity probe, since a plain
# `systemctl --user status` with no session bus just hangs/errors the same
# way a real failure would.
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_DST="$SYSTEMD_USER_DIR/modelmanager.service"
mkdir -p "$SYSTEMD_USER_DIR"
if [[ ! -f "$UNIT_DST" ]]; then
  cp "$REPO_DIR/systemd/modelmanager.service" "$UNIT_DST"
else
  echo "$UNIT_DST already exists - leaving it alone."
fi
if command -v systemctl >/dev/null && systemctl --user daemon-reload >/dev/null 2>&1; then
  systemctl --user enable --now modelmanager.service
  echo "Coordinator running as a systemd --user service (check: systemctl --user status modelmanager.service)."
  echo "If this box might reboot unattended (no graphical login happening automatically), also run once: loginctl enable-linger $(whoami)"
else
  echo "warning: no user systemd session available - couldn't start the coordinator automatically." >&2
  echo "  Run it yourself: modelmanager serve --port 8778 (see README: \"Run the coordinator\")" >&2
fi

# Same trick pipx uses for exactly this problem: an isolated venv keeps the
# tool's deps out of the way, but nothing puts its bin/ on PATH on its own -
# without this, every bare `modelmanager ...` in this README would 404 on a
# fresh install. ~/.local/bin is on PATH by default on most distros (XDG
# convention); symlinking there (not copying) means it always points at
# whatever's in the venv, including after a future dependency update.
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
ln -sf "$VENV_DIR/bin/modelmanager" "$LOCAL_BIN/modelmanager"
echo "Linked $LOCAL_BIN/modelmanager -> $VENV_DIR/bin/modelmanager"

case ":$PATH:" in
  *":$LOCAL_BIN:"*) ;;
  *)
    echo "warning: $LOCAL_BIN isn't on your PATH - add it in your shell profile" >&2
    echo "  (e.g. echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc), then restart your shell" >&2
    ;;
esac


# skills/modelmanager/SKILL.md teaches a coding agent how to drive the REST
# API/CLI to set up a first model - but it's only ever read if it lives
# somewhere the agent actually scans itself: a personal skill directory
# (~/.claude/skills/<name>, ~/.codex/skills/<name> - same symlink convention
# Omarchy's own skills already use on this machine). Asking first, rather
# than just dropping a symlink outside this repo unasked, since that's a
# change to the user's agent setup, not this plugin's own config. Skipped
# entirely when not running interactively (piped install, CI, etc.).
SKILL_SRC="$REPO_DIR/skills/modelmanager"
if [[ -t 0 && -t 1 ]]; then
  echo ""
  echo "Link the modelmanager skill into a coding agent, so it can set up your first model for you?"
  echo "  1) Claude Code (~/.claude/skills/modelmanager)"
  echo "  2) Codex (~/.codex/skills/modelmanager)"
  echo "  3) None"
  read -r -p "Choice [1/2/3, default 3]: " SKILL_CHOICE || SKILL_CHOICE=3
  case "$SKILL_CHOICE" in
    1) SKILL_DST="$HOME/.claude/skills/modelmanager" ;;
    2) SKILL_DST="$HOME/.codex/skills/modelmanager" ;;
    *) SKILL_DST="" ;;
  esac
  if [[ -n "$SKILL_DST" ]]; then
    if [[ -L "$SKILL_DST" && "$(readlink -f "$SKILL_DST")" == "$(readlink -f "$SKILL_SRC")" ]]; then
      echo "$SKILL_DST already links here - leaving it alone."
    elif [[ -e "$SKILL_DST" ]]; then
      echo "warning: $SKILL_DST already exists and isn't a link to this skill - not touching it" >&2
    else
      mkdir -p "$(dirname "$SKILL_DST")"
      ln -s "$SKILL_SRC" "$SKILL_DST"
      echo "Linked $SKILL_DST -> $SKILL_SRC"
    fi
  fi
else
  echo ""
  echo "Not running interactively, so skipping the agent-skill prompt. Link it"
  echo "  yourself later, e.g.: ln -s $SKILL_SRC ~/.claude/skills/modelmanager"
fi

echo "Done. 'modelmanager' is ready to use (new shell/session may be needed if PATH was just updated above)."
