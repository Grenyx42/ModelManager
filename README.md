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
  [Authentication & TLS](docs/deployment.md#authentication--tls).
  `scripts/setup.sh` generates both automatically; there's no "running
  open" mode.
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
[Authentication & TLS](docs/deployment.md#authentication--tls)) - all of it lives at a fixed
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
  [Authentication & TLS](docs/deployment.md#authentication--tls) for the
  same mechanism already used for the token/TLS paths). A custom port only takes effect
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
(by design - see the [Omarchy plugin docs](https://plugins.omarchy.org/publish.html)).
Enabling the plugin before running `./scripts/setup.sh` is fine though - the
panel detects the backend isn't up yet and shows a "Run setup now" button
right there, instead of just sitting empty. While the registry's still
unconfigured it also offers, as checkboxes applied by that same button:
linking [`skills/modelmanager/SKILL.md`](skills/modelmanager/SKILL.md) into
a coding agent, and registering a model you already have pulled in Ollama
(see `scripts/register-ollama-model.sh` - it doesn't download anything,
just points the registry at whichever model `ollama list` already shows).
`scripts/install-omarchy-plugin.sh` also checks for this and warns if it's
missing, for the dev-sync workflow below.

### Run the coordinator

`./scripts/setup.sh` (see [Setup](#setup) above) already installs
`systemd/modelmanager.service` as a user unit and starts it for you.

```bash
modelmanager serve --port 8778   # binds 0.0.0.0 by default - reachable from systemB, systemC, etc.
```

`--host`/`--port` default to the `MODELMANAGER_HOST`/`MODELMANAGER_PORT` env
vars when set, else `0.0.0.0`/`8778` - a flag always wins over either.
Serves HTTPS and refuses to start without a TLS cert (see
[Authentication & TLS](docs/deployment.md#authentication--tls)); every
request also needs a bearer token, so "reachable" isn't the same as
"usable." See [docs/deployment.md](docs/deployment.md) for the manual
systemd install, overriding the listen address via an `EnvironmentFile`,
reaching the coordinator from another machine, and Windows boxes.

### CLI

```bash
modelmanager doctor --pretty    # run this first: system reachability + is every model's binary/model file actually there
modelmanager list               # all model definitions + live status, --pretty for a human-readable view
modelmanager ensure qwen-7b     # start/swap as needed, prints {"host","port"}
modelmanager stop qwen-7b
modelmanager help               # full registry schema + API reference, for agents/scripts
```

The full command list - leases (`acquire`/`release`), groups, logs,
dependency resolution, dry-run, `validate`, `deploy-agent`, access/lifecycle
logs - is in [docs/cli-reference.md](docs/cli-reference.md).

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
separate step needed to register it with omarchy-shell. Developing the
plugin itself (editing `Panel.qml`/`manifest.json` in your own working
copy, re-syncing a live install) is covered in
[CONTRIBUTING.md](CONTRIBUTING.md).

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
you start editing it into your own. Field-level details not obvious from
the example - `pinned` models, per-engine `env`/`cwd`, self-managed Ollama
builds - are in [docs/registry-schema.md](docs/registry-schema.md).

## Dependencies

`fastapi`, `uvicorn`, `httpx`, and `pydantic` - all MIT/BSD-licensed. No
bundled/vendored third-party code beyond what `pip` installs from PyPI.

## Contributing

Bug reports and PRs welcome - see [CONTRIBUTING.md](CONTRIBUTING.md) for how
to run the tests and what a PR should look like.

## License

[MIT](LICENSE)
