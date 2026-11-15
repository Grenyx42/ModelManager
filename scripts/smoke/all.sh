#!/bin/bash
# One command, all four tiers, sane defaults for this machine - run before
# committing/pushing anything that touches install/CLI/coordinator/runner
# code or Panel.qml:
#
#   scripts/smoke/all.sh
#
# Defaults to exercising sleek's nomic-embed (cleared for unrestricted
# stop/start testing) and the live bar widget. Anything after `--` is passed
# straight through to run.sh, to override the defaults or add flags, e.g.:
#
#   scripts/smoke/all.sh -- --model nomic-embed --skip-install   # skip tier 1
#   scripts/smoke/all.sh -- --model nomic-embed --system sleek   # no --ui, no stop/start
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# `all.sh --help` (or any other typo/unrecognized argument) used to fall
# straight through to the destructive
# default below instead of showing help or erroring - exactly the one
# command you'd expect to be safe to run with a flag you got wrong.
case "${1:-}" in
  "") exec bash "$SCRIPT_DIR/run.sh" --model nomic-embed --system sleek --allow-stop-start --ui ;;
  --) shift; exec bash "$SCRIPT_DIR/run.sh" "$@" ;;
  -h|--help)
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 0
    ;;
  *)
    echo "error: unrecognized argument '$1' - pass flags after a '--' to forward them to run.sh (see --help)" >&2
    exit 1
    ;;
esac
