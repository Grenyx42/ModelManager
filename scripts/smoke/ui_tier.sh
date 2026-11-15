#!/bin/bash
# Tier 4 smoke test: catch obvious breakage in the live Omarchy bar widget -
# QML warnings/errors on load, and a data consistency check between what the
# panel is actually displaying (via its `.debug` IPC target, see Panel.qml's
# dumpState()) and a fresh, independent `modelmanager` CLI call for the same
# model. This is the one tier that touches the real, already-running bar -
# not a disposable sandbox like install_tier.sh - because QML rendering
# behavior can't be exercised any other way (see CONTRIBUTING.md's
# "Developing the Omarchy plugin" section for why a plain rescan/enable
# cycle isn't enough to pick up an edit; omarchy-restart-shell is required).
#
# Usage: scripts/smoke/ui_tier.sh [model-name]
# Omits the data-consistency check (just the warning scan) if no model name
# is given.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODEL="${1:-}"
export MODEL
PASS=0
FAIL=0

check() {
  if [[ "$2" == "0" ]]; then echo "  OK    $1"; PASS=$((PASS + 1))
  else echo "  FAIL  $1"; FAIL=$((FAIL + 1)); fi
}

echo "== tier 4: live bar-widget smoke test =="

echo "Syncing current working copy into the live plugin..."
bash "$REPO_DIR/scripts/install-omarchy-plugin.sh" >/dev/null

echo "Restarting omarchy-shell to force a real reload (rescan/enable alone won't pick up a QML edit)..."
omarchy-restart-shell >/dev/null 2>&1
sleep 3

# By config path, not bare process name - pgrep -n quickshell would happily
# hand back a *different* quickshell instance (e.g. a one-off `quickshell
# ipc`/test invocation) if one happened to start after the restart, and
# every check below would then silently query the wrong shell.
PID="$(pgrep -nf "quickshell .*-p /usr/share/omarchy/shell" || true)"
if [[ -z "$PID" ]]; then
  echo "  FAIL  omarchy-shell is running after restart"
  echo "1 passed, 1 failed"
  exit 1
fi
echo "  OK    omarchy-shell is running after restart (pid $PID)"
PASS=$((PASS + 1))

echo "Checking for QML warnings/errors in the plugin's own files..."
if LOG_OUTPUT="$(quickshell log -p /usr/share/omarchy/shell --tail 200 2>&1)"; then
  LOG_HITS="$(printf '%s\n' "$LOG_OUTPUT" | grep -iE "warn|error" | grep -i "modelmanager" || true)"
  if [[ -z "$LOG_HITS" ]]; then
    check "no QML warnings/errors from this plugin" 0
  else
    echo "  FAIL  QML warnings/errors found:"
    printf '%s\n' "$LOG_HITS" | sed 's/^/        /'
    FAIL=$((FAIL + 1))
  fi
else
  check "quickshell log command succeeds" 1
fi

echo "Checking the panel registered its debug IPC target..."
if quickshell ipc --pid "$PID" show 2>&1 | grep -q "io.github.grenyx42.modelmanager.debug"; then
  check "dumpState IPC target registered" 0
else
  check "dumpState IPC target registered" 1
fi

STATE="$(quickshell ipc --pid "$PID" call io.github.grenyx42.modelmanager.debug dumpState 2>&1 || true)"
# Found by Claude Code's review: this used to assume exactly 2 checks ran
# (`&& PASS+=2 || { PASS+=1; FAIL+=1; }`) - but the except branch below
# prints only 1 line (not 2) when JSON parsing fails outright, so that
# path was crediting a phantom PASS alongside the real FAIL. Count the
# OK/FAIL lines actually printed instead of assuming a fixed number.
DUMPSTATE_OUT="$(echo "$STATE" | python3 -c "
import json, sys
try:
    state = json.load(sys.stdin)
except Exception as exc:
    print(f'  FAIL  dumpState returned valid JSON ({exc})')
    sys.exit(1)

ok = True
w, h = state.get('implicitWidth'), state.get('implicitHeight')
if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
    print(f'  OK    panel rendered at a real size ({w}x{h})')
else:
    print(f'  FAIL  panel implicitWidth/Height is zero or missing (w={w!r} h={h!r}) - the icon/button likely isn\'t painting anything')
    ok = False

print('  OK    dumpState returned valid JSON')
sys.exit(0 if ok else 1)
" || true)"
echo "$DUMPSTATE_OUT"
PASS=$((PASS + $(grep -c "^  OK" <<<"$DUMPSTATE_OUT" || true)))
FAIL=$((FAIL + $(grep -c "^  FAIL" <<<"$DUMPSTATE_OUT" || true)))

# Checking shutil.which("modelmanager") (this
# shell's PATH) isn't good enough - Panel.qml's mmBin resolves independently
# of PATH (MODELMANAGER_BIN env var, else a fixed venv path; see Panel.qml),
# so this shell could have a fresh dev install on PATH while the panel
# itself is still running an older one. Check the exact binary the panel
# itself reported via dumpState.
MM_BIN="$(echo "$STATE" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('mmBin') or '')
except Exception:
    print('')
")"
# The panel can be pointed at a remote
# coordinator (root.activeHost, a `hosts add`-ed peer name - see
# mmPeerArgs() in Panel.qml), in which case there's no reason a *local*
# modelmanager.service even has to be running - only the local CLI
# binary's files matter there (it's still what builds the --peer
# request). Requiring a local running process in that case would fail a
# perfectly healthy panel.
ACTIVE_HOST="$(echo "$STATE" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('activeHost') or '')
except Exception:
    print('')
")"
# check_fresh_install.py's own freshness
# check reads MODELMANAGER_URL from ITS environment (this shell's), not
# the panel's - Quickshell/Hyprland's session env (which is what the panel
# actually runs under) can set MODELMANAGER_URL/PORT differently. Pass the
# panel's own reported localCoordinatorUrl through explicitly, same
# reasoning as passing it to the CLI comparison calls further down.
LOCAL_URL="$(echo "$STATE" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('localCoordinatorUrl') or '')
except Exception:
    print('')
")"
FRESH_CHECK_ARGS=("$MM_BIN")
if [[ -n "$ACTIVE_HOST" ]]; then
  FRESH_CHECK_ARGS+=(--skip-process-check)
fi
if [[ -z "$MM_BIN" ]]; then
  check "panel reported its own mmBin" 1
elif MODELMANAGER_URL="${LOCAL_URL:-${MODELMANAGER_URL:-}}" python3 "$REPO_DIR/scripts/smoke/check_fresh_install.py" "${FRESH_CHECK_ARGS[@]}"; then
  check "panel's mmBin ($MM_BIN) matches this working copy" 0
else
  check "panel's mmBin ($MM_BIN) matches this working copy" 1
fi

if [[ -n "$MODEL" ]]; then
  # This script's own `modelmanager list`
  # always read the *local* coordinator, even when the panel itself (per
  # ACTIVE_HOST above) is showing a remote peer's data. Comparing two
  # different coordinators' data could spuriously fail, or spuriously
  # pass if they happened to agree - match whichever one the panel
  # itself is actually showing.
  echo "Cross-checking the panel's displayed data against a fresh CLI call for '$MODEL'${ACTIVE_HOST:+ (peer: $ACTIVE_HOST)}..."
  # dumpState reads the panel's own cached
  # root.models, which only updates on its own refreshIntervalSec timer
  # (user-configurable, default 15s - see Panel.qml) - a fixed, short
  # retry window could keep reading the panel's stale pre-refresh value
  # the entire time. Size the window off the panel's own reported
  # interval instead of guessing a constant.
  REFRESH_SEC="$(echo "$STATE" | python3 -c "
import json, sys
try:
    print(int(json.load(sys.stdin).get('refreshIntervalSec') or 15))
except Exception:
    print(15)
")"
  ATTEMPTS=$(( (REFRESH_SEC + 10) / 2 + 1 ))
  # FOUND/MATCHED, not a single pass/fail: the initial CLI lookup used to
  # be a one-shot check outside this loop, so a merely transient failure
  # (the CLI momentarily unreachable) permanently failed the tier instead
  # of getting the same retries everything else here gets.
  FOUND=1
  MATCHED=1
  for _ in $(seq 1 "$ATTEMPTS"); do
    FRESH_STATE="$(quickshell ipc --pid "$PID" call io.github.grenyx42.modelmanager.debug dumpState 2>&1 || true)"
    # Deriving this fresh every iteration,
    # not once before the loop - if the panel switches hosts mid-retry,
    # the CLI call must follow it, not keep querying whatever host it
    # started on.
    FRESH_ACTIVE_HOST="$(echo "$FRESH_STATE" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('activeHost') or '')
except Exception:
    print('')
")"
    FRESH_LOCAL_URL="$(echo "$FRESH_STATE" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('localCoordinatorUrl') or '')
except Exception:
    print('')
")"
    FRESH_CLI_PEER_ARGS=()
    if [[ -n "$FRESH_ACTIVE_HOST" ]]; then
      FRESH_CLI_PEER_ARGS=(--peer "$FRESH_ACTIVE_HOST")
    elif [[ -n "$FRESH_LOCAL_URL" ]]; then
      FRESH_CLI_PEER_ARGS=(--url "$FRESH_LOCAL_URL")
    fi
    # This must run the panel's own mmBin,
    # not whatever bare `modelmanager` resolves to on this shell's PATH -
    # those can be different binaries (MM_BIN was already checked for
    # freshness above). Guarded with an `if` (not a bare assignment) so a
    # transient failure or invalid JSON doesn't abort the whole script
    # under `set -euo pipefail` - it just falls through to the retry below.
    if FRESH_CLI_ENTRY="$("$MM_BIN" "${FRESH_CLI_PEER_ARGS[@]}" list 2>/dev/null | python3 -c "
import json, os, sys
models = json.load(sys.stdin)
entry = next((m for m in models if m['name'] == os.environ['MODEL']), None)
print(json.dumps(entry) if entry else '')
")"; then
      :
    else
      FRESH_CLI_ENTRY=""
    fi
    if [[ -z "$FRESH_CLI_ENTRY" ]]; then
      sleep 2
      continue
    fi
    FOUND=0
    if CLI_ENTRY="$FRESH_CLI_ENTRY" python3 -c "
import json, os, sys
panel = json.load(sys.stdin)
model = os.environ['MODEL']
cli_entry = json.loads(os.environ['CLI_ENTRY'])
panel_entry = next((m for m in panel.get('models', []) if m.get('name') == model), None)
if panel_entry is None:
    sys.exit(1)
mismatches = [k for k in ('active', 'pinned') if panel_entry.get(k) != cli_entry.get(k)]
sys.exit(1 if mismatches else 0)
" <<<"$FRESH_STATE"; then
      MATCHED=0
      break
    fi
    sleep 2
  done
  check "model '$MODEL' found via CLI to compare against" "$FOUND"
  if [[ "$FOUND" -eq 0 ]]; then
    check "panel's displayed state for '$MODEL' matches the CLI (retried on mismatch)" "$MATCHED"
  fi
else
  echo "(no model name given - skipping data-consistency check)"
fi

echo ""
echo "tier 4: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
