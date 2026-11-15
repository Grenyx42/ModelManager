#!/bin/bash
# Tier 1 smoke test: does a real, from-scratch install actually work.
#
# Mirrors scripts/setup.sh's own install steps (requirements.lock with hash
# verification, a plain --no-deps package install, registry.clean.json,
# a generated token, a generated TLS cert) but entirely inside a disposable
# HOME/venv and a manually-run `modelmanager serve` - never systemd, never
# ~/.local/bin, never the skill-linking prompt - so this can run as often as
# you like without touching the real install on this box. Catches packaging/
# wiring regressions (missing deps, a broken entry point, a CLI that can't
# even start) - not hardware-sensitive behavior, see hardware_tier.py for that.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_DIR="$(mktemp -d /tmp/modelmanager-smoke-install.XXXXXX)"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
SERVE_PID=""

cleanup() {
  if [[ -n "$SERVE_PID" ]] && kill -0 "$SERVE_PID" 2>/dev/null; then
    kill "$SERVE_PID" 2>/dev/null || true
    wait "$SERVE_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

PASS=0
FAIL=0

check() {
  local label="$1"; shift
  if "$@" >"$WORK_DIR/last.out" 2>&1; then
    echo "  OK    $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $label"
    sed 's/^/        /' "$WORK_DIR/last.out"
    FAIL=$((FAIL + 1))
  fi
}

echo "== tier 1: clean install (workdir: $WORK_DIR) =="

VENV_DIR="$WORK_DIR/venv"

# XDG_CONFIG_HOME alone isn't enough isolation - any of these, inherited
# from the real shell profile (scripts/setup.sh
# itself suggests exporting MODELMANAGER_PORT there), overrides it or a
# derived default in modelmanager/config.py, and XDG_STATE_HOME (used by
# access_log.py/lifecycle_log.py) defaults to the real ~/.local/state if not
# set here - so without this, this "disposable" coordinator could load the
# real registry.json (and start its pinned models) or write into the real
# install's logs.
unset MODELMANAGER_REGISTRY MODELMANAGER_TOKEN_PATH MODELMANAGER_GRANTS_PATH \
  MODELMANAGER_KNOWN_HOSTS_PATH MODELMANAGER_KNOWN_HOSTS_DIR \
  MODELMANAGER_TLS_CERT_PATH MODELMANAGER_TLS_KEY_PATH \
  MODELMANAGER_HOST MODELMANAGER_PORT MODELMANAGER_TOKEN
export XDG_CONFIG_HOME="$WORK_DIR/config"
export XDG_STATE_HOME="$WORK_DIR/state"
export MODELMANAGER_URL="https://127.0.0.1:$PORT"

python3 -m venv "$VENV_DIR"
echo "  (created venv)"

LOCK="$REPO_DIR/requirements.lock"
check "pip install -r requirements.lock (hash-verified)" \
  "$VENV_DIR/bin/pip" install -q --require-hashes -r "$LOCK"
check "pip install modelmanager itself (no-deps, like setup.sh)" \
  "$VENV_DIR/bin/pip" install -q --no-deps "$REPO_DIR"

MM="$VENV_DIR/bin/modelmanager"
check "entry point exists and runs (--help)" "$MM" --help

mkdir -p "$XDG_CONFIG_HOME/modelmanager"
cp "$REPO_DIR/registry.clean.json" "$XDG_CONFIG_HOME/modelmanager/registry.json"
"$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_hex(32))' \
  > "$XDG_CONFIG_HOME/modelmanager/token"

TLS_DIR="$XDG_CONFIG_HOME/modelmanager/tls"
mkdir -p "$TLS_DIR"
openssl req -x509 -newkey rsa:4096 -sha256 -days 1 -nodes \
  -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
  -subj "/CN=modelmanager-smoke-test" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
  >/dev/null 2>&1
echo "  (generated throwaway registry/token/TLS cert under $XDG_CONFIG_HOME)"

"$MM" serve --host 127.0.0.1 --port "$PORT" >"$WORK_DIR/serve.log" 2>&1 &
SERVE_PID=$!

READY=0
for _ in $(seq 1 30); do
  if "$MM" version >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.5
done
if [[ "$READY" == "1" ]]; then
  echo "  OK    coordinator came up on 127.0.0.1:$PORT"
  PASS=$((PASS + 1))
else
  echo "  FAIL  coordinator never became reachable - serve.log:"
  sed 's/^/        /' "$WORK_DIR/serve.log"
  FAIL=$((FAIL + 1))
fi

check "version command" "$MM" version
check "list (empty registry)" "$MM" list
check "systems (empty registry)" "$MM" systems
check "doctor" "$MM" doctor
check "config" "$MM" config
check "hosts list (no hosts yet)" "$MM" hosts list

echo ""
echo "tier 1: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
