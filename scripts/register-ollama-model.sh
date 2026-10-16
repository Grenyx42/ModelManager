#!/bin/bash
# Registers one Ollama model the user already has pulled, as a minimal
# single-model registry entry - run from the bar widget's first-time-setup
# panel (an opt-in checkbox) once the coordinator is reachable.
#
# Deliberately does not download anything: anyone installing this plugin
# almost certainly already has Ollama (and/or llama.cpp) running models -
# that's the whole reason to want a coordinator in front of them. This just
# points the coordinator at whichever model Ollama's own /api/tags lists
# first, on the assumption Ollama is running on this same machine at its
# default port (not configurable here - hand-edit registry.json, or ask
# your coding agent, for anything more specific).
set -euo pipefail

MM="${1:?usage: register-ollama-model.sh <modelmanager-bin-path>}"

command -v curl >/dev/null || { echo "curl not found - can't check for an existing Ollama model" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq not found - can't parse Ollama's model list" >&2; exit 1; }

# Prefers a model Ollama itself tags "completion" (skips embedding-only
# models like nomic-embed-text, which would 400 on the /api/generate call
# ensure() makes) - falls back to the very first model if Ollama's response
# doesn't include capabilities at all (older versions).
MODEL=$(curl -s --max-time 3 http://localhost:11434/api/tags 2>/dev/null | jq -r '
  (.models | map(select((.capabilities // []) | index("completion"))) | .[0].name)
  // .models[0].name // empty
') || true
if [[ -z "$MODEL" ]]; then
  echo "No Ollama models found at http://localhost:11434 - is Ollama installed, running, and has at least one model pulled (ollama pull ...)?" >&2
  exit 1
fi

# registry.json keys don't need sanitizing (any string works), but a
# literal ":" in a model name like "llama3.2:1b" is easy to mistype on a
# command line later - kebab-case it for the model's own key, while
# model_ref below keeps the exact tag Ollama itself expects.
KEY=$(echo "$MODEL" | tr ':' '-')

put() {
  local kind="$1" name="$2" body="$3" out
  if ! out=$("$MM" put "$kind" "$name" "$body" 2>&1); then
    echo "failed to create $kind/$name: $out" >&2
    exit 1
  fi
}

put systems local '{"os":"linux","control":"local"}'
put engines ollama-local '{"system":"local","type":"ollama","base_url":"http://localhost:11434"}'
put gpu_groups local-gpu0 '{"system":"local"}'
put models "$KEY" "{\"candidates\":[{\"engine\":\"ollama-local\",\"gpu_group\":\"local-gpu0\",\"port\":11434,\"model_ref\":\"$MODEL\"}]}"

echo "Registered existing Ollama model '$MODEL' as '$KEY' - try: modelmanager ensure $KEY"
