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
modelmanager ensure qwen-72b      # start/swap as needed, prints {"host","port"}
modelmanager stop qwen-72b
modelmanager logs qwen-72b --lines 100
modelmanager systems              # configured systems
modelmanager stats monolith       # GPU/VRAM via nvidia-smi (local or SSH)
```

### Windows boxes

Copy `scripts/windows/agent.ps1` to `C:\ModelManager\agent.ps1` on each
Windows system referenced in `registry.json` with `"control": "ssh"`. Make
sure `ssh <that-host>` already works non-interactively (key auth, no
passphrase prompt) - `~/.ssh/config` aliases are reused as-is.

### Omarchy plugin

See `omarchy/plugin/README.md`.

## Registry

`registry.json` (gitignored; start from `registry.example.json`) is the
source of truth: which systems exist, which engines run on them, and which
model definitions map to which engine+system candidates, with `pinned` and
`idle_timeout_seconds` controlling eviction. See the example file for the
full shape.
