# ModelManager

Coordinator for LLM inference engines (Ollama, custom llama.cpp builds)
running across several GPU machines (`sleek`, `monolith`, `coder`), plus an
Omarchy bar plugin to see and control them.

## Pieces

- `modelmanager/` - the Python coordinator: a JSON registry of
  systems/engines/models/rules, a FastAPI REST service, and the
  local/SSH+PowerShell control paths for Ollama and llama.cpp.
- `scripts/windows/agent.ps1` - deployed to each Windows box; the coordinator
  invokes it over SSH to start/stop/status/log native llama.cpp processes.
- `omarchy/plugin/` - a Quickshell/Omarchy bar-widget plugin (QML) that shells
  out to the `modelmanager` CLI for a bar icon + panel.
- `systemd/modelmanager.service` - user unit to run the coordinator at login.

See `../.claude/plans/zany-puzzling-glacier.md` (or ask Claude) for the full
design writeup.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp registry.example.json registry.json   # then edit paths/models for real
pytest
```

### Run the coordinator

```bash
modelmanager serve --port 8778
```

Or install `systemd/modelmanager.service` as a user unit so pinned models
(e.g. an embedding model) come up automatically:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/modelmanager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now modelmanager.service
```

Adjust `ExecStart`/`MODELMANAGER_REGISTRY` in that unit if you're not using a
`~/.local/bin` install or the default repo path.

### CLI

```bash
modelmanager list                 # all model definitions + live status
modelmanager ensure qwen-72b      # start/swap as needed, prints {"host","port"} - no usage lease
modelmanager acquire qwen-72b --ttl 300   # like ensure, but takes a lease: can't be evicted/swapped while held
modelmanager release qwen-72b             # end a lease (or all leases if --lease-id omitted)
modelmanager stop qwen-72b                # --force to stop even if a lease is held
modelmanager logs qwen-72b --lines 100
modelmanager systems              # configured systems
modelmanager stats monolith       # GPU/VRAM via nvidia-smi (local or SSH)
modelmanager help                 # full registry schema + API reference, for agents/scripts
modelmanager put models qwen-72b '{"candidates": [...]}'   # create/update an entry (see `help`)
modelmanager put engines my-engine @path/to/body.json      # body can also come from a file
modelmanager config                                        # full current registry as one document
modelmanager reload                                        # re-read registry.json from disk, apply live
modelmanager deploy-agent [system] [--force]               # sync scripts/windows/agent.ps1 to windows/ssh boxes
```

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
removed, drops cached adapters so changed engine/system config takes effect,
and auto-starts anything newly pinned.

**Acquire/release vs ensure:** `ensure` just makes sure a model is loaded and
refreshes its idle clock - a concurrent idle sweep or another model's request
can still evict it once `idle_timeout_seconds` passes, even mid-use. `acquire`
additionally takes a time-boxed lease (default 300s, renewable by acquiring
again) that makes the model un-evictable and un-swappable while held, for
callers doing a longer piece of work; `release` ends the lease and starts the
idle clock fresh from that moment instead of from the original acquire.
Leases expire on their own if never released, so a crashed caller can't
permanently pin a GPU slot.

### Windows boxes

Nothing to copy by hand: the coordinator deploys `scripts/windows/agent.ps1`
itself to `~\.ModelManager\agent.ps1` (the SSH user's own home directory - the
exact `C:\Users\<name>` path is resolved over SSH, never assumed) on every
Windows system with `"control": "ssh"`, automatically and best-effort on
every startup, skipping the upload if the file's already there unchanged.
To push a change without restarting, or target one box:

```bash
modelmanager deploy-agent            # every windows/ssh system
modelmanager deploy-agent monolith   # just one
modelmanager deploy-agent monolith --force   # re-upload even if unchanged
```

Just make sure `ssh <that-host>` already works non-interactively (key auth,
no passphrase prompt) - `~/.ssh/config` aliases are reused as-is.

### Omarchy plugin

See `omarchy/plugin/README.md`.

## Registry

`registry.json` (gitignored; start from `registry.example.json`) is the
source of truth: which systems exist, which engines run on them, and which
model definitions map to which engine+system candidates, with `pinned` and
`idle_timeout_seconds` controlling eviction. See the example file for the
full shape.
