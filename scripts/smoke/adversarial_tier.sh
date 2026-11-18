#!/bin/bash
# Tier 5 smoke test: an LLM agent, not a human or a fixed script, tries to
# break a live coordinator any way it can - API calls, modelmanager
# cmdlines, malformed input, auth-boundary probing - and the test is
# "pass" only if it finds nothing. This is deliberately NOT part of
# scripts/smoke/all.sh's default run: unlike tiers 1-4, it costs real LLM
# time/spend per run and isn't deterministic the way a hypothesis property
# test or a fixed assertion is, so it belongs as an occasional gate (before
# a marketplace resubmission, or after a security-sensitive change), not in
# the fast loop that runs on every commit.
#
# This script only sets up and tears down the sandbox - it does NOT invoke
# the agent itself (deliberately: the agent needs real tool access with a
# human/Claude session supervising the result, not a bare
# --dangerously-skip-permissions subprocess with no one watching). Usage:
#
#   scripts/smoke/adversarial_tier.sh start   # sets up the sandbox, prints
#                                              # a JSON briefing (url, certs,
#                                              # owner token, a pre-made
#                                              # scoped grant) to stdout and
#                                              # leaves the coordinator
#                                              # running in the background
#   scripts/smoke/adversarial_tier.sh stop    # tears it down
#
# Typical flow: run `start`, hand the printed briefing to a fresh agent
# along with scripts/smoke/adversarial_prompt.txt, let it probe, review
# whatever it reports yourself (treat ANY claimed finding as a reason to
# dig in by hand before deciding it's real - same rule as every other
# review this project runs), then run `stop`.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_FILE="/tmp/modelmanager-smoke-adversarial.state"

cmd_start() {
  if [[ -f "$STATE_FILE" ]]; then
    echo "error: $STATE_FILE already exists - a sandbox may already be running. Run 'stop' first (or rm the state file if it's stale)." >&2
    exit 1
  fi

  local work_dir
  work_dir="$(mktemp -d /tmp/modelmanager-smoke-adversarial.XXXXXX)"
  local port
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"

  # Same isolation as install_tier.sh - a fresh HOME-equivalent, never the
  # real ~/.config/modelmanager, never systemd. This sandbox is deliberately
  # disposable and safe to fuzz destructively - that's the whole point.
  unset MODELMANAGER_REGISTRY MODELMANAGER_TOKEN_PATH MODELMANAGER_GRANTS_PATH \
    MODELMANAGER_KNOWN_HOSTS_PATH MODELMANAGER_KNOWN_HOSTS_DIR \
    MODELMANAGER_TLS_CERT_PATH MODELMANAGER_TLS_KEY_PATH \
    MODELMANAGER_HOST MODELMANAGER_PORT MODELMANAGER_TOKEN
  export XDG_CONFIG_HOME="$work_dir/config"
  export XDG_STATE_HOME="$work_dir/state"
  export MODELMANAGER_URL="https://127.0.0.1:$port"

  local venv_dir="$work_dir/venv"
  python3 -m venv "$venv_dir" >/dev/null
  "$venv_dir/bin/pip" install -q --require-hashes -r "$REPO_DIR/requirements.lock"
  "$venv_dir/bin/pip" install -q --no-deps "$REPO_DIR"
  local mm="$venv_dir/bin/modelmanager"

  local config_dir="$XDG_CONFIG_HOME/modelmanager"
  mkdir -p "$config_dir"
  cp "$REPO_DIR/scripts/smoke/adversarial_registry.json" "$config_dir/registry.json"
  local owner_token
  owner_token="$("$venv_dir/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
  printf '%s\n' "$owner_token" > "$config_dir/token"

  local tls_dir="$config_dir/tls"
  mkdir -p "$tls_dir"
  openssl req -x509 -newkey rsa:4096 -sha256 -days 1 -nodes \
    -keyout "$tls_dir/key.pem" -out "$tls_dir/cert.pem" \
    -subj "/CN=modelmanager-smoke-adversarial" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    >/dev/null 2>&1

  "$mm" serve --host 127.0.0.1 --port "$port" >"$work_dir/serve.log" 2>&1 &
  local serve_pid=$!

  local ready=0
  for _ in $(seq 1 30); do
    if "$mm" version >/dev/null 2>&1; then ready=1; break; fi
    sleep 0.5
  done
  if [[ "$ready" != "1" ]]; then
    echo "error: coordinator never became reachable - serve.log:" >&2
    sed 's/^/  /' "$work_dir/serve.log" >&2
    kill "$serve_pid" 2>/dev/null || true
    rm -rf "$work_dir"
    exit 1
  fi

  # One pre-made scoped grant, limited to "sleek" only - "vault",
  # "private-dep", "mismatched"'s gpu_group, and "needs-dep"'s dependency
  # chain are all meant to stay out of reach of this token specifically, so
  # an agent that has both tokens has a real boundary to try to cross, not
  # just an empty registry to describe.
  local grant_token
  grant_token="$("$venv_dir/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
  printf '%s\n' "$grant_token" | "$mm" grant add fuzzpeer >/dev/null
  "$mm" grant allow fuzzpeer sleek >/dev/null

  cat > "$STATE_FILE" <<EOF
{
  "work_dir": "$work_dir",
  "serve_pid": $serve_pid,
  "venv_mm": "$venv_dir/bin/modelmanager",
  "xdg_config_home": "$XDG_CONFIG_HOME",
  "cert_path": "$tls_dir/cert.pem",
  "url": "$MODELMANAGER_URL",
  "owner_token": "$owner_token",
  "grant_name": "fuzzpeer",
  "grant_token": "$grant_token",
  "grant_allowed_systems": ["sleek"],
  "note": "To use the modelmanager CLI: export XDG_CONFIG_HOME=<xdg_config_home> MODELMANAGER_URL=<url>, then run <venv_mm> <command> --token <owner_token or grant_token>. For raw HTTP: curl --cacert <cert_path> -H 'Authorization: Bearer <token>' <url><path>."
}
EOF
  echo "$work_dir" > "$work_dir/.workdir-marker"

  echo "Sandbox is up. Briefing (also at $STATE_FILE):"
  cat "$STATE_FILE"
}

cmd_stop() {
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "no sandbox state found at $STATE_FILE - nothing to stop"
    return 0
  fi
  local work_dir serve_pid
  work_dir="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["work_dir"])' "$STATE_FILE")"
  serve_pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["serve_pid"])' "$STATE_FILE")"
  if kill -0 "$serve_pid" 2>/dev/null; then
    kill "$serve_pid" 2>/dev/null || true
    wait "$serve_pid" 2>/dev/null || true
  fi
  rm -rf "$work_dir"
  rm -f "$STATE_FILE"
  echo "sandbox torn down"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 1
    ;;
esac
