# ModelManager

ModelManager tracks which LLM models are actually running, on which of your
machines, and starts/stops/swaps them on request - so an app (or you) can
ask for a model by name (`"qwen-72b"`) and get back a live `host:port` to
talk to, without caring whether it's already loaded, which box it's on, or
what has to be evicted first to make room. An Omarchy bar plugin sits on top
of the same REST API for a point-and-click view of what's running.

It grew out of my own home-lab setup: a handful of GPU boxes (Linux,
Windows), each able to run one or more LLM engines (Ollama, custom llama.cpp
builds), and apps that needed one stable name to call instead of remembering
which box happens to have `qwen-72b` loaded this week.

> Built mostly by pairing with Claude (Anthropic's Claude Code) over a long
> series of sessions - most of the code, tests, and docs started there. I've
> since made it my own and will keep extending it by hand, so don't be
> surprised if some corners read more "assistant" than "human" for a while.

## What it is / isn't

- It's a small REST coordinator: apps call `ensure("model-name")`, it starts
  (or swaps in) whichever engine/box combo serves that model, and hands back
  `{host, port}`. It does **not** sit in the inference request path - your
  app talks to the engine directly after that.
- It assumes a **trusted private network** (e.g. your own LAN or a Tailscale
  tailnet) between your boxes, and SSH between the coordinator and any remote
  system is a local environment decision you make yourself (non-interactive
  key auth already working, `~/.ssh/config` aliases) - ModelManager doesn't
  set that up or manage it. Don't expose the coordinator to the open internet.
- The REST API requires a bearer token over HTTPS on every request - see
  [Authentication & TLS](#authentication--tls) below. `scripts/setup.sh`
  generates both automatically; there's no "running open" mode.
- It doesn't download or manage model weights - you point it at
  files/builds you already have.
- The Omarchy bar plugin is optional and genuinely Omarchy-specific
  (Quickshell/Hyprland), but the coordinator + CLI underneath it are plain
  Python and work fine headless, without Omarchy, on Linux or as the
  controlling side of a Windows box over SSH.

```
Apps, or the Omarchy bar plugin
        │  REST: ensure("model-name") / verify / stop
        ▼
  Coordinator (FastAPI)  ── one JSON registry: systems, engines, models
        │
        ▼
  EngineAdapter (Ollama | llama.cpp)  ──  Runner (local subprocess | SSH+PowerShell)
        │
        ▼
  Your GPU boxes - Linux local or remote, Windows always over SSH
```

The coordinator hands back `{host, port}`; your app then talks to the engine
directly. Nothing proxies through the coordinator after that handoff.

## Requirements

| | Why |
|---|---|
| Python 3.11+ | runs the coordinator/CLI |
| [Omarchy](https://omarchy.org) (Hyprland + omarchy-shell) | only if you want the bar plugin - skip it and just use the CLI/REST API otherwise |
| Ollama and/or your own llama.cpp (`llama-server`) builds | on whichever machines you want to manage |
| Non-interactive SSH (key auth, no passphrase prompt) | only for Windows boxes under your control - the coordinator controls them over SSH + PowerShell, no agent to install |

## Pieces

- `modelmanager/` - the Python coordinator: a JSON registry of
  systems/engines/models/rules, a FastAPI REST service, and the
  local/SSH+PowerShell control paths for Ollama and llama.cpp.
- `scripts/windows/agent.ps1` - deployed to each Windows box; the coordinator
  invokes it over SSH to start/stop/status/log native llama.cpp processes.
- `manifest.json` / `Panel.qml` - a Quickshell/Omarchy bar-widget plugin that
  shells out to the `modelmanager` CLI for a bar icon + panel. These live at
  the repo root (not a subdirectory) because the Omarchy plugin marketplace
  requires `manifest.json` at a repo's root.
- `systemd/modelmanager.service` - user unit to run the coordinator at login.

## Setup

```bash
./scripts/setup.sh
```

This builds a runtime venv at `~/.local/share/modelmanager/venv`, an empty
starter config at `~/.config/modelmanager/registry.json` (copied from
`registry.clean.json`), a REST API bearer token at
`~/.config/modelmanager/token`, and a self-signed TLS cert/key at
`~/.config/modelmanager/tls/` (see
[Authentication & TLS](#authentication--tls)) - all of it lives at a fixed
location outside this repo, not inside it, since this repo might be a
throwaway dev clone, or it might be wherever `omarchy plugin
add` happened to put it, and either way it can be replaced wholesale on an
update. It then installs `systemd/modelmanager.service` as a user unit and
starts it, so the coordinator is actually running by the time the script
finishes - see [Run the coordinator](#run-the-coordinator) - and symlinks
`modelmanager` itself onto `~/.local/bin` (on PATH by default on most
distros), so every bare `modelmanager ...` command below just works. Re-run
`./scripts/setup.sh` any time, it's idempotent (an existing registry.json,
token, TLS cert, or env file is left alone).

Run interactively (not piped, not CI), it also asks two things, each
skippable:

- Whether to customize the coordinator's listen host/port (default
  `0.0.0.0:8778`) - writes `~/.config/modelmanager/env`, read by the
  systemd unit above as an `EnvironmentFile` (see
  [Authentication & TLS](#authentication--tls) for the same mechanism
  already used for the token/TLS paths). A custom port only takes effect
  for that systemd-managed process - also set `MODELMANAGER_PORT` wherever
  the CLI or bar widget run locally (see [Omarchy plugin](#omarchy-plugin));
  the script says so at the time if you pick a non-default port.
- Whether to symlink [`skills/modelmanager/SKILL.md`](skills/modelmanager/SKILL.md)
  into a coding agent's personal skills directory (Claude Code, Codex, or
  skip) - it never does this without asking, since it's touching your agent
  setup, not just this plugin's own config; say no and link it yourself
  later, or just paste the file into a chat.

Then describe your actual systems/engines/models - that part can't be
automated, it's your own machines - either by telling that agent to set up
a model, or by hand, using `registry.example.json` as a full reference shape.

Once you've edited it and started the coordinator (see below),
`modelmanager doctor --pretty` is the fastest way to confirm it's all
actually wired up correctly - every system reachable, and every model's
binary/model file genuinely present on disk, not just referenced by a
well-formed path.

Dependencies install from `requirements.lock` with `--require-hashes` -
every version pinned, every artifact's digest verified, so a future or
compromised PyPI release can't land unreviewed in a service that's likely
running on every login. Regenerate it with `pip-compile --generate-hashes
--output-file=requirements.lock pyproject.toml` (needs `pip install pip-tools`)
whenever you change a dependency in `pyproject.toml`.

`./scripts/uninstall.sh` reverses this: stops/removes the systemd service and
the runtime venv, but leaves `registry.json` alone.

If you're developing this project rather than just running it, you'll also
want a normal repo-local venv for running the tests (see
[CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

**Note for the Omarchy plugin:** `Panel.qml` just shells out to the venv
`scripts/setup.sh` creates; there's no install hook that runs this for you
(by design - see the [Omarchy plugin docs](https://plugins.omarchy.org/publish.html)),
so run `./scripts/setup.sh` *before* enabling the plugin, or it'll enable but
show nothing. `scripts/install-omarchy-plugin.sh` checks for this and warns
if it's missing.

### Run the coordinator

`./scripts/setup.sh` (see [Setup](#setup) above) already installs
`systemd/modelmanager.service` as a user unit and starts it for you - this
section is for running it some other way, or customizing that unit.

```bash
modelmanager serve --port 8778   # binds 0.0.0.0 by default - reachable from systemB, systemC, etc.
```

`--host`/`--port` default to the `MODELMANAGER_HOST`/`MODELMANAGER_PORT` env
vars when set, else `0.0.0.0`/`8778` - a flag always wins over either. Serves
HTTPS (self-signed cert from `./scripts/setup.sh` - see
[Authentication & TLS](#authentication--tls)) and refuses to start at all if
that cert doesn't exist yet. Binding `0.0.0.0` means the REST API is
*reachable* by anything that can reach this box - on this setup that's the
tailnet, gated by Tailscale itself rather than anything in this app - but
every request still needs the bearer token below, so reachable isn't the
same as usable. Pass `--host 127.0.0.1` (or set `MODELMANAGER_HOST=127.0.0.1`)
instead if you want it local-only as well.

`systemA`'s system entry in `registry.json` needs `"advertise_host": "systemA"`
(its Tailscale name) for this to actually work end-to-end: without it,
`ensure`/`acquire` on a model running locally would hand a remote caller
back `{"host": "localhost", ...}`, which means *them*, not systemA.

If you ever need to install it by hand instead (the unit so pinned models,
e.g. an embedding model, come up automatically):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/modelmanager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now modelmanager.service
```

That unit already points at the fixed venv/registry locations `scripts/setup.sh`
and `default_registry_path()` use, so it works as-is regardless of where this
repo was cloned. It also reads `~/.config/modelmanager/env` as an
`EnvironmentFile` (missing is fine, nothing requires it) - put
`MODELMANAGER_HOST=...`/`MODELMANAGER_PORT=...`/`MODELMANAGER_REGISTRY=...`/etc.
in there (`scripts/setup.sh` offers to write the first two interactively) to
override any of `serve`'s defaults without touching the unit file itself,
then `systemctl --user daemon-reload && systemctl --user restart modelmanager`.

A `systemctl --user` unit only starts once *some* login session for that user
exists - fine for a desktop machine you're normally logged into, but if this
box might reboot unattended (no graphical login happening automatically),
also run `loginctl enable-linger $(whoami)` once so the user service manager
itself starts at boot regardless, and the coordinator (and any pinned models)
comes back without you logging in first.

### Authentication & TLS

Every REST request needs `Authorization: Bearer <token>` over HTTPS - no
exception for a "just reading" endpoint, and no way to run the coordinator
without both configured. `./scripts/setup.sh` generates both automatically
on first run - the token at `~/.config/modelmanager/token` (0600), a
self-signed TLS cert/key at `~/.config/modelmanager/tls/` (key 0600) - same
XDG-style fixed location as `registry.json`. The CLI picks both up with no
configuration of its own, and since `Panel.qml` only ever shells out to the
CLI, the bar plugin authenticates (and verifies TLS) for free too - this
only matters to you if something *other* than the CLI/plugin needs to call
the REST API directly (your own app, a script):

```bash
modelmanager auth show      # print the current token (and where it came from)
modelmanager auth rotate    # generate and switch to a new one, no restart needed
```

A few things worth knowing:

- **`MODELMANAGER_TOKEN`** (env var) overrides the token file entirely, for
  both the coordinator and the CLI - useful for a systemd `EnvironmentFile`
  (`~/.config/modelmanager/env` - see [Run the coordinator](#run-the-coordinator))
  or a container secret instead of a file on disk. Likewise
  `MODELMANAGER_TLS_CERT_PATH`/`MODELMANAGER_TLS_KEY_PATH` for the cert/key,
  and `MODELMANAGER_HOST`/`MODELMANAGER_PORT` for what `serve` binds to.
- **Rotating the token takes effect immediately** - the coordinator reads it
  fresh on every request rather than caching it at startup, so there's
  nothing to restart after `modelmanager auth rotate`. Update whatever else
  holds the old value (another app, a saved `curl` command) at the same time,
  since it'll start getting 401s the instant you rotate.
- **The token doesn't expire** - it's valid until you manually rotate it.
  That's a deliberate simplicity tradeoff for a single-operator home-lab
  tool, not an oversight; treat it like an SSH private key, not a
  low-stakes API key, since holding it means being able to start/stop
  anything and rewrite the registry (see the architecture notes above on
  what that implies for remote systems).
- **The cert is self-signed** (there's no public CA to ask for a box that
  only your own machines ever talk to) and covers `localhost`, `127.0.0.1`,
  and this machine's own hostname - not a Tailscale MagicDNS name, which
  `setup.sh` has no reliable way to know in advance. A caller on another box
  needs its own copy of `tls/cert.pem` to verify against (the CLI falls back
  to the system CA bundle if it doesn't have one, which will fail closed
  with a clear TLS error, not silently accept an unverified connection).
- **An existing install upgrading into this version** won't have a
  token/cert yet, and the coordinator will refuse to start (missing cert) or
  refuse every request with a clear `AUTH_NOT_CONFIGURED` error (missing
  token) until they exist - re-run `./scripts/setup.sh` (safe, idempotent),
  or `modelmanager auth rotate` for just the token.
- This closes the "anyone on the LAN can read or hit the coordinator's REST
  API" gap, but it's not a substitute for a trusted network - keep the
  coordinator off the open internet regardless.

**Planned:** today it's one token with full access to everything. Scoped,
per-token access to specific models is on the roadmap - mainly so a model
can be shared with friends on your tailnet without handing them the keys to
your whole fleet.

### Using `modelmanager` from another machine

The CLI defaults to `https://127.0.0.1:8778` - i.e. "the coordinator is on
this same box" - which is correct for the box actually running it, but not
for a second machine (another box of yours, or a friend's, on the same
tailnet) that wants to call a coordinator running elsewhere. Three things
make that work, all copied from the coordinator box's
`~/.config/modelmanager/`:

1. **Point it at the right host.** Set `MODELMANAGER_URL` to the
   coordinator's reachable address - its Tailscale name/IP, not `localhost`:
   ```bash
   export MODELMANAGER_URL=https://systemA:8778
   ```
   Unset (the default), every `modelmanager` command stays local-only - it
   never silently reaches across the network for you.
2. **Copy the token** (`~/.config/modelmanager/token` on the coordinator
   box) and set it on this one:
   ```bash
   export MODELMANAGER_TOKEN=<the token value - `modelmanager auth show` on the coordinator box prints it>
   ```
3. **Copy the cert** (`~/.config/modelmanager/tls/cert.pem` - not `key.pem`,
   that one never leaves the coordinator box) somewhere on this machine and
   point at it:
   ```bash
   export MODELMANAGER_TLS_CERT_PATH=/path/to/your/copy/of/cert.pem
   ```
   Skip this and the CLI falls back to the system CA bundle, which won't
   recognize a self-signed cert - every call fails closed with a clear TLS
   error rather than silently trusting whatever's on the other end.

All three are plain env vars, so a shell profile or `.env` on that second
machine is enough to make every bare `modelmanager ...` command there behave
exactly like it does locally on the coordinator box. None of this applies to
the Omarchy bar plugin - it only ever talks to a coordinator running on its
own box (see the architecture notes near the top), so it has nothing to
configure here.

### CLI

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

### Groups

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

### Windows boxes

Nothing to copy by hand: the coordinator deploys `scripts/windows/agent.ps1`
itself to `~\.ModelManager\agent.ps1` (the SSH user's own home directory - the
exact `C:\Users\<name>` path is resolved over SSH, never assumed) on every
Windows system with `"control": "ssh"`, automatically and best-effort on
every startup, skipping the upload if the file's already there unchanged.
To push a change without restarting, or target one box:

```bash
modelmanager deploy-agent            # every windows/ssh system
modelmanager deploy-agent systemB    # just one
modelmanager deploy-agent systemB --force    # re-upload even if unchanged
```

Just make sure `ssh <that-host>` already works non-interactively (key auth,
no passphrase prompt) - `~/.ssh/config` aliases are reused as-is.

**Why `agent.ps1` launches via WMI instead of `Start-Process`:** Windows'
OpenSSH assigns every SSH session's processes to a Job Object, and kills the
whole job the instant the session closes - which is right after `agent.ps1`
returns. A plain `Start-Process` child stays in that job and gets killed
along with it, so the model would appear to start successfully and then die
within moments, often with zero log output if it happened mid-load. WMI
process creation (`Invoke-CimMethod ... Win32_Process Create`) makes
`WmiPrvSE.exe` the parent instead, entirely outside that job, so the process
survives past the SSH session ending - the actual point of starting a
background service. Confirmed directly: a `Start-Process`-launched PID was
gone the moment a fresh SSH connection checked it; a WMI-launched one wasn't.

### Omarchy plugin

Bar icon (running-model count) and click-through panel for the coordinator.
Pure display + action layer: all state comes from shelling out to the
`modelmanager` CLI, which talks to the coordinator's local REST API. No QML
file talks to SSH, engines, or the registry directly.

`Panel.qml` calls the CLI by its venv's absolute path, not the bare
command - `omarchy-shell` runs with Hyprland's session environment from
login, not an interactive terminal's `PATH` after activating a venv, so a
bare `"modelmanager"` in a `Process { command: [...] }` reliably fails to
start. It defaults to `~/.local/share/modelmanager/venv/bin/modelmanager`
(what `scripts/setup.sh` creates); set `MODELMANAGER_BIN` in your Hyprland
session environment if you've put it somewhere else on purpose. Same for
`MODELMANAGER_PORT`/`MODELMANAGER_URL` if you've customized the
coordinator's listen port (see [Run the coordinator](#run-the-coordinator))
- the systemd `EnvironmentFile` that changes only affects the coordinator
process itself, not this one, so the bar widget (and a plain CLI call from
a terminal) still needs it set in whatever environment they run in too.

**Installing it:** if you just want to use the plugin (not develop it),
clone this repo or `omarchy plugin add <this-repo-url> --enable`, then run
`scripts/setup.sh` from wherever it ended up. That's the whole install - no
separate step needed to register it with omarchy-shell.

**If you're developing the plugin itself** (editing `Panel.qml` or
`manifest.json` in your own working copy), `scripts/install-omarchy-plugin.sh`
re-syncs your live edits into `~/.config/omarchy/plugins/<id>` each time you
run it:

```bash
scripts/install-omarchy-plugin.sh
```

That syncs this repo's plugin-relevant files to `~/.config/omarchy/plugins/<id>`
(idempotent - safe to re-run; re-run it after editing `Panel.qml` to pick up
the change), validates the manifest, reloads omarchy-shell's plugin registry,
and enables it. A plain symlink isn't used here because `omarchy plugin
validate` rejects any symlink inside the plugin folder, and a dev checkout's
own `.venv` (for running tests - see CONTRIBUTING.md; unrelated to the
runtime venv `scripts/setup.sh` creates) always has some. Default placement
lands it in the bar's center section; move it with:

```bash
omarchy bar move <id> --section right
```

(`<id>` is the `id` field in `manifest.json`.)

**Editing `Panel.qml`:** the "saving a file hot-reloads it" behavior turned
out to be unreliable in practice - a `sed -i` edit (which rewrites via a
temp file + rename) silently stopped the file-watcher from firing at all,
and even `omarchy-shell shell rescanPlugins` and a disable/enable cycle kept
running the previously-compiled version afterwards. If a change doesn't
seem to take effect within a few seconds of saving, don't trust
rescan/enable to fix it - run `omarchy-restart-shell` (kills and relaunches
the one long-running quickshell process; safe, and preserves layout/lock
state) and check `quickshell log -p /usr/share/omarchy/shell --tail 50` for
warnings/errors afterwards.

**Settings:** `refreshIntervalSec` (default 15) controls how often the model
list refreshes. Per-system GPU stats and per-model logs are fetched on
demand (clicking Stats/Logs), not on the refresh timer, since a remote
system's stats go over SSH and shouldn't be polled continuously.

```bash
omarchy bar set <id> refreshIntervalSec 30 --json
```

**Keybinding:** there's no manifest field or plugin-level mechanism for
registering a global hotkey - Hyprland keybindings are entirely yours to
own, in `~/.config/hypr/bindings.lua`. The plugin already exposes
`open`/`close`/`toggle` over Quickshell's IPC (`ipcTarget: "slideout.modelmanager"`,
inherited from the base `Panel` type - no code here needed to support this),
so toggling the panel from a key is one line, the same pattern any other
Omarchy plugin uses:

```lua
o.bind("SUPER + ALT + M", "ModelManager", "omarchy-shell shell toggle slideout.modelmanager")
```

Pick whatever key you like - `omarchy menu keybindings --print` shows
what's already taken.

## Registry

`registry.json` is the source of truth: which systems exist, which engines
run on them, and which model definitions map to which engine+system
candidates, with `pinned` and `idle_timeout_seconds` controlling eviction.
`scripts/setup.sh` creates it at `~/.config/modelmanager/registry.json` from
`registry.clean.json` - empty (no systems/engines/models), so a fresh
install boots with nothing to misconfigure, not someone else's machines;
override the location with `MODELMANAGER_REGISTRY`. See `registry.example.json`
for the full shape, or point your coding agent at
[`skills/modelmanager/SKILL.md`](skills/modelmanager/SKILL.md) to fill in
your first model for you.

`registry.example.json` is a real, working registry - it's a sanitized copy
of my own (machine names and a stray local path changed), not a toy snippet,
so it's worth skimming for how the pieces actually fit together (multiple
engines per system, several models sharing a `gpu_group`, a `group`) before
you start editing it into your own.

None of the example file's models are `pinned`, deliberately - a pinned
model auto-starts the moment the coordinator boots (see "Acquire/release vs
ensure" above), which would fail loudly on a brand-new install before you've
edited anything to match your own machines. Once you've pointed a model at
something real, a standing dependency like an always-on embedding model
looks like this:

```json
"nomic-embed": {
  "pinned": true,
  "candidates": [
    { "engine": "ollama-systemA", "model_ref": "nomic-embed-text", "port": 11434, "gpu_group": "systemA-gpu0-alwaysOn", "ollama_task": "embed" }
  ]
}
```

An engine can set `"env": {...}` - extra environment variables merged in
when the coordinator launches its process - and `"cwd": "..."` - the
working directory to launch it from, if that matters for the build (e.g. it
resolves a relative library path from there). For llama.cpp this always
applies. Common `env` use: `LD_LIBRARY_PATH`, when a build's CUDA/cuBLAS
libraries live somewhere non-standard (e.g. bundled inside a Python venv via
a pip `nvidia-*` package rather than installed system-wide) - the
coordinator launches processes with its own environment, not whatever a dev
shell happened to have set.

An `ollama` engine normally just talks to `base_url`, assuming a server is
already running there (a system/Windows service, outside our control). Set
`"binary"` (and usually `"cwd"`/`"env"`) on it too if that's not true - e.g.
a custom Ollama build that isn't registered as a service anywhere - and the
coordinator launches and manages `ollama serve` itself the first time a
model on that engine is started, the same way it manages a raw llama.cpp
process (PID/log file locally, or the Windows WMI-launcher agent over SSH).
`base_url` still needs to be a host the coordinator can actually reach
(e.g. `http://systemB:11434`, not `localhost`, when the server lives on a
different box), and `OLLAMA_HOST` in `env` needs to match the port and bind
address you want it listening on.

## Dependencies

`fastapi`, `uvicorn`, `httpx`, and `pydantic` - all MIT/BSD-licensed. No
bundled/vendored third-party code beyond what `pip` installs from PyPI.

## Contributing

Bug reports and PRs welcome - see [CONTRIBUTING.md](CONTRIBUTING.md) for how
to run the tests and what a PR should look like.

## License

[MIT](LICENSE)
