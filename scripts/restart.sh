#!/bin/bash
# Stops and restarts the ModelManager coordinator on this box (sleek).
# Pure Python app - no build step, just the venv's console script.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELMANAGER="$REPO_DIR/.venv/bin/modelmanager"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/modelmanager"
PID_FILE="$STATE_DIR/coordinator.pid"
LOG_FILE="$STATE_DIR/coordinator.log"
HOST="${MODELMANAGER_HOST:-127.0.0.1}"
PORT="${MODELMANAGER_PORT:-8778}"

mkdir -p "$STATE_DIR"

if [[ ! -x "$MODELMANAGER" ]]; then
  echo "error: $MODELMANAGER not found - run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "Stopping coordinator (pid $pid)..."
    kill "$pid"
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "Still running after 4s, sending SIGKILL..."
      kill -9 "$pid"
    fi
  fi
  rm -f "$PID_FILE"
fi

echo "Starting coordinator on $HOST:$PORT..."
nohup "$MODELMANAGER" serve --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
new_pid=$!
echo "$new_pid" > "$PID_FILE"

sleep 1
if kill -0 "$new_pid" 2>/dev/null; then
  echo "Started (pid $new_pid). Logs: $LOG_FILE"
else
  echo "Failed to start - check $LOG_FILE" >&2
  exit 1
fi
