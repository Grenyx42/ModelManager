# CLI reference

The full command list, groups, dependency resolution, and lease semantics.
`modelmanager help` (or `GET /v1/help`) is always authoritative over this
file - see the main [README](../README.md) for installation and a
five-command quickstart.

## Commands

```bash
modelmanager doctor --pretty      # run this first: system reachability + is every model's binary/model file actually there
modelmanager list                 # all model definitions + live status, --pretty for a human-readable view
modelmanager ensure qwen-7b      # start/swap as needed, prints {"host","port"} - no usage lease
modelmanager ensure qwen-7b --dry-run --pretty   # preview what ensure would do (keep/start/swap/blocked) without doing it
modelmanager verify qwen-7b --pretty     # confirm it's actually answering requests, not just "started"
modelmanager acquire qwen-7b --ttl 300   # like ensure, but takes a lease: can't be evicted/swapped while held
modelmanager release qwen-7b             # end a lease (or all leases if --lease-id omitted)
modelmanager stop qwen-7b                # --force to stop even if a lease is held
modelmanager logs qwen-7b --lines 100
modelmanager log-path qwen-7b     # {system,control,os,ssh_host,path} - where its logs actually live, for tailing directly
modelmanager systems              # configured systems
modelmanager stats systemB --pretty       # GPU/VRAM via nvidia-smi (local or SSH)
modelmanager help                 # full registry schema + API reference, for agents/scripts
modelmanager version              # API/wire-contract version
modelmanager validate @registry.json      # check a full registry document without applying it (catches every error in one pass)
modelmanager put models qwen-7b '{"candidates": [...]}'   # create/update an entry (see `help`)
modelmanager put engines my-engine @path/to/body.json      # body can also come from a file
modelmanager config                                        # full current registry as one document
modelmanager reload                                        # re-read registry.json from disk, apply live
modelmanager deploy-agent [system] [--force]               # sync scripts/windows/agent.ps1 to windows/ssh boxes
modelmanager access-log --lines 100                         # recent REST requests: client, method, path, status, duration
modelmanager lifecycle-log --lines 100                       # recent model crash/restart events, across every model
```

A model definition can also declare `"depends_on": ["other-model-name"]` -
`ensure`/`acquire` resolve every dependency first, recursively, before doing
anything with the model itself (see `modelmanager help` for the full field
description and cycle-detection rules).

## Groups

A group is a named set of existing model names (often spanning several
systems) that belong to one workflow, so you can start/check/stop all of
them with a single name instead of one call per model:

```bash
modelmanager put groups pi-dev '["coding-bonsai-27b", "agentflow-planner-3b", "decision-4b"]'
modelmanager groups                        # list all groups
```

`ensure`, `verify`, `stop`, and `stats` all accept a group name directly - no
separate command needed:

```bash
modelmanager ensure pi-dev      # best-effort: ensure() every member independently
modelmanager verify pi-dev --pretty
modelmanager stop pi-dev
modelmanager stats pi-dev --pretty   # one entry per distinct system behind the group's models, not per model
```

Each is best-effort: one member's failure (offline box, gpu_group conflict,
etc.) never blocks the rest, and the result reports every member
individually. The explicit `group-ensure`/`group-verify`/`group-stop`/
`group-stats` commands do the same thing and exist for scripts that want to
be unambiguous about targeting a group rather than relying on the fallback.

Incoming requests are logged separately from app logs, to
`~/.local/state/modelmanager/access.log` (rotated at 5MB, 3 backups) -
readable directly on systemA, via the CLI above, or `GET /v1/access-log` for a
caller on another machine with no shell access to systemA.

A background health check polls every active model's status every 5s; if
one's active but no longer running (a crash - `--n-gpu-layers` mismatch, an
OOM, whatever), it's restarted automatically via the same recovery path
`ensure()` already used lazily, and the event is recorded to
`~/.local/state/modelmanager/lifecycle.log` (same rotation, same CLI/REST
pattern as the access log above). A model's own log (`logs`/`log-path`) still
has the actual crash reason in its raw stdout/stderr; this is the compact,
cross-model "when did X die and did it come back" timeline that's otherwise
missing since there's no standalone crash marker in a raw process log.
Note this is a genuine crash detector, not a substitute for
`idle_timeout_seconds` - a model stopped by the idle sweep or an explicit
`stop` has already cleared its own active state before this check ever runs,
so it's left alone. Neither engine has its own idle-unload timer under our
control: llama.cpp is a raw process with no timeout unless you add one to a
candidate's own `args`, and the Ollama adapter always loads with
`keep_alive=-1` and unloads explicitly - so an unexpectedly-dead model here is
never "it expired," always "it crashed."

`GET /v1/help` is meant to be self-sufficient: any harness, agent, or script can hit it
first to learn the systems/engines/gpu_groups/models schema (fields, types,
required/optional, examples) and every endpoint, then `POST /v1/{resource}/{name}`
to create or update an entry directly - no need to hand-edit `registry.json`.
Writes validate the whole registry (including cross-references) before
accepting anything and persist atomically; an invalid write returns a
specific 422 error and changes nothing.

There's no delete endpoint by design: hand-edit `registry.json` to remove an
entry, then `modelmanager reload` to apply it without restarting the
coordinator. Reload stops any currently-active model whose definition was
actually removed, drops cached adapters so changed engine/system config
takes effect, and auto-starts anything newly pinned.

Renaming an active model's key (instead of removing it) is safe too: reload
detects a new, unfamiliar key with the exact same `candidates` as a
disappeared one and treats it as a rename, carrying over the running
state (active candidate, last-used clock, leases) instead of stopping the
process and leaving the new name with no memory of it. This only kicks in
for a model that was actually active at reload time and whose candidates
match exactly - change the port/model/engine in the same edit and it's
treated as a real remove+add instead.

**Acquire/release vs ensure:** `ensure` just makes sure a model is loaded and
refreshes its idle clock - a concurrent idle sweep or another model's request
can still evict it once `idle_timeout_seconds` passes, even mid-use. Leaving
`idle_timeout_seconds` null doesn't mean "never evicted" - the idle sweep
leaves it alone forever (no timer to check), but another model's `ensure()`
can still swap it out of its `gpu_group` the instant it actually needs that
slot, with no idle grace period to wait out; only an active lease blocks
that. Use `pinned: true` for a model that must never be evicted by anything.
`acquire`
additionally takes a time-boxed lease (default 300s, renewable by acquiring
again) that makes the model un-evictable and un-swappable while held, for
callers doing a longer piece of work; `release` ends the lease and starts the
idle clock fresh from that moment instead of from the original acquire.
Leases expire on their own if never released, so a crashed caller can't
permanently pin a GPU slot.
