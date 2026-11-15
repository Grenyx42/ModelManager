#!/bin/bash
# High-level smoke test runner - not a replacement for `pytest`, which checks
# correctness at the unit level. This checks the things only a real install
# on real hardware can catch: packaging/wiring regressions (tier 1) and
# live-hardware/model regressions (tier 2/3, e.g. the GPU-less CPU/RAM stats
# bug, which only showed up on airy's actual hardware).
#
# Usage:
#   scripts/smoke/run.sh                                           # tier 1 only
#   scripts/smoke/run.sh --model nomic-embed --allow-stop-start    # + tier 2/3, local
#   scripts/smoke/run.sh --model nomic-embed --peer sleek --allow-stop-start --skip-install
#   scripts/smoke/run.sh --model nomic-embed --ui                  # + tier 4 (live bar widget)
#
# Any extra flags (--model/--peer/--url/--system/--allow-stop-start/
# --verify-timeout) are forwarded as-is to hardware_tier.py. Pass
# --skip-install to skip tier 1 (e.g. when iterating quickly on tier 2/3 alone).
# --ui runs tier 4 too (only makes sense against this box's own live bar -
# it restarts omarchy-shell, so never pass it alongside --peer/--url).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SKIP_INSTALL=0
RUN_UI=0
REMOTE_TARGET=0
HARDWARE_ARGS=()
MODEL=""
require_value() {
  # --model/--peer/--url with no
  # following value used to read "$2" unguarded under `set -u`, crashing
  # with bash's opaque "$2: unbound variable" instead of a clean error.
  if [[ "$#" -lt 2 ]]; then
    echo "error: $1 requires a value" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --ui) RUN_UI=1; shift ;;
    --model) require_value "$@"; MODEL="$2"; HARDWARE_ARGS+=("$1" "$2"); shift 2 ;;
    --model=*) MODEL="${1#--model=}"; HARDWARE_ARGS+=("$1"); shift ;;
    --peer|--url) require_value "$@"; REMOTE_TARGET=1; HARDWARE_ARGS+=("$1" "$2"); shift 2 ;;
    --peer=*|--url=*) REMOTE_TARGET=1; HARDWARE_ARGS+=("$1"); shift ;;
    *) HARDWARE_ARGS+=("$1"); shift ;;
  esac
done

# --ui always checks *this* box's own live
# bar, regardless of --peer/--url - it never covers the remote target those
# flags are asking tier 2/3 to hit, so the combination's result wouldn't
# mean what it looks like it means.
if [[ "$RUN_UI" -eq 1 && "$REMOTE_TARGET" -eq 1 ]]; then
  echo "error: --ui only checks this box's own live bar - never pass it with --peer/--url" >&2
  exit 1
fi

# This used to run after tier 1 (a full venv
# build + pip install) had already finished - a typo like --modle wasted
# that time before ever getting reported. Catch it before anything runs.
if [[ -z "$MODEL" && "${#HARDWARE_ARGS[@]}" -gt 0 ]]; then
  echo "error: hardware-tier flags given (${HARDWARE_ARGS[*]}) but no --model - typo? (argparse would reject this itself if it ran)" >&2
  exit 1
fi

OVERALL=0

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  "$SCRIPT_DIR/install_tier.sh" || OVERALL=1
  echo ""
else
  echo "== tier 1: skipped (--skip-install) =="
  echo ""
fi

if [[ -n "$MODEL" ]]; then
  python3 "$SCRIPT_DIR/hardware_tier.py" "${HARDWARE_ARGS[@]}" || OVERALL=1
else
  echo "== tier 2/3: skipped (no --model given) =="
fi
echo ""

if [[ "$RUN_UI" -eq 1 ]]; then
  bash "$SCRIPT_DIR/ui_tier.sh" "$MODEL" || OVERALL=1
else
  echo "== tier 4: skipped (no --ui given) =="
fi

exit "$OVERALL"
