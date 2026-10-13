## Contributing

Thanks for taking a look. This started as a personal home-lab tool, so a few
things are still tuned for that - PRs that generalize things are welcome.

# Built fast. Please break it.

I built this the way a lot of us build now - a spec and then a working prototype in 30 minutes.  It got useful **really fast** because I'm always tinkering with what models can fit on my GPUs and work together.  It is almost certainly missing something obvious I didn't think of yet.  If you find a missing feature, weird edge case, or something that makes you say *"Dude... how did you not add this?"* - open an issue.  

Really.

We're living in a world where a feature that used to mean "maybe I can build that next weekend" can usually mean "give me five minutes". So don't assume something is too small, too weird, or too much trouble to suggest.

This is a tool for people experimenting with local AI, built using the same tools we're all experimenting with.

**Find the gaps. Break stuff. Suggest things. Let's make it better.**


## Architecture: one coordinator, SSH out

ModelManager runs as a single coordinator process on one machine (in my case,
the same box running the Omarchy desktop/plugin) and reaches every other
machine (`systemB`, `systemC`, ...) over SSH. There's exactly one `registry.json`,
one process deciding what starts/stops/evicts, and the other machines run
nothing but an engine (Ollama, llama.cpp) and an SSH server.

The alternative would be a coordinator/plugin instance running on *every*
machine, each one talking to its peers instead of SSHing into them. That
would remove the SSH dependency, but it trades it for three harder problems:

- A Windows/WSL build of the plugin and coordinator, not just Linux/Omarchy
  (today the remote machines only need SSH + the engine - nothing of this
  codebase runs on them).
- Keeping `registry.json` in sync across every peer.
- Locking/consensus so two coordinators can't both decide to start or evict
  the same model at the same time - the single `threading.Lock()` in
  `Manager` only works because there's exactly one process making decisions.

For an Omarchy-specific tool, single-site + SSH was the simpler, smaller
surface to build and keep correct. If you want multi-site/HA coordination,
that's a real design change (peer sync, consensus, a non-Linux build) - open
an issue to talk through the approach before sending a PR for it.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

No real `registry.json`, GPU, or remote box is needed to run the test suite -
`modelmanager/`, `coordinator/`, and `adapters/` are unit tested against a
fake `Runner`/registry fixture (see `tests/test_manager.py::build_registry`),
not real processes or SSH.

## Scope of the test suite

`modelmanager/` (config, coordinator, adapters, runners) is unit tested and
any change there should come with tests. `modelmanager/cli.py` is a thin
`httpx` wrapper around the REST API and is intentionally *not* covered by
`tests/` - it's verified by hand against a running coordinator. If you're
changing CLI output formatting, describe how you tested it manually in the PR.

The Omarchy plugin (`manifest.json`, `Panel.qml`) has no automated tests - it
shells out to the CLI and renders QML, neither of which is practical to unit
test here. Describe what you clicked and what you expected to see.

### Scenarios worth thinking through for lifecycle/orchestration changes

- Model already running and healthy.
- Process running but inference failing.
- SSH connection failure.
- Remote runtime missing.
- Port conflict.
- Startup timeout.
- Insufficient available resources.
- Concurrent `ensure` requests.
- Partial group failure.
- Manager restart with remote models still running.
- Windows vs. Linux command differences.

## Behavioral invariants

- JSON-first output: CLI responses stay machine-readable by default; `-p` is
  opt-in pretty formatting.
- Real readiness verification: a running process or open port doesn't prove
  readiness - `verify` must make an actual inference request.
- Idempotent `ensure`: repeated calls must not unnecessarily restart healthy
  services or create duplicate processes.
- Accurate endpoints: returned hostnames/ports/URLs must describe the actual
  reachable service.
- Group transparency: group operations expose individual results and partial
  failures, never a single opaque success/failure.
- Runtime independence: don't assume every model uses Ollama, llama.cpp, or
  any single engine.
- Cross-platform compatibility: preserve Linux and Windows remote execution
  behavior.
- Safe process ownership: never terminate an unrelated process just because
  it occupies a configured port.
- Actionable errors: structured enough for a human or coding agent to
  interpret, not just "something failed."
- Centralized orchestration: the REST service stays the authoritative
  coordinator - don't duplicate orchestration logic inside clients.
- Authenticated by default: every REST route requires a valid bearer token -
  no "read-only" or "discovery" exemptions, no way to run the coordinator
  with auth off. A missing/wrong token and a server with no token configured
  at all are distinct, specific errors (`UNAUTHORIZED` vs
  `AUTH_NOT_CONFIGURED`), never a silent allow.

## Before opening a PR

See the PR template's checklist for the baseline (tests pass, new behavior is
covered, schema docs stay in sync). A couple of things it doesn't cover:

- If you changed a dependency in `pyproject.toml`, regenerate
  `requirements.lock` and commit it:
  ```bash
  pip install pip-tools
  pip-compile --generate-hashes --no-index --output-file=requirements.lock pyproject.toml
  ```
  `scripts/setup.sh` installs from this lock with `--require-hashes`, so a
  dependency change that isn't reflected here will fail for anyone who runs
  it fresh.
- Keep changes scoped - this project avoids speculative abstractions and
  unused config knobs; a small, focused PR is much easier to review than one
  that also reorganizes nearby code.
- Prefer explicit service states over ambiguous booleans.
- Don't change an existing response schema without documenting the
  compatibility implications.
- Keep CLI and REST behavior consistent with each other.

## Security

- Never hardcode SSH credentials, tokens, or passwords.
- Never print secrets in logs or JSON output - the one deliberate exception
  is `modelmanager auth show`, whose entire purpose is displaying the local
  REST API token for the owner to copy elsewhere; it never goes through
  access_log/lifecycle_log or the REST API itself (see `cmd_auth` in
  `cli.py`), so it doesn't leak anywhere persistent.
- Treat remote execution as a privileged operation.
- Validate configuration and command inputs.
- Don't silently modify remote system configuration.
- The REST API requires a bearer token over HTTPS on every request, with no
  exemptions (see `install_auth_middleware` in
  `modelmanager/coordinator/app.py`, `cmd_serve`'s TLS wiring in `cli.py`,
  and the README's [Authentication & TLS](README.md#authentication--tls)
  section) - both the auth requirement and the TLS requirement are security
  boundaries, so changes to either (new exempt routes, weaker comparison,
  an http:// fallback, etc.) need the same scrutiny as any other
  security-relevant change. Still not a substitute for network controls:
  don't expose the coordinator to the open internet regardless.
- The coordinator's TLS cert is self-signed and generated locally
  (`scripts/setup.sh`, via `openssl`) - there's no CA, no revocation, no
  expiry monitoring. That's a deliberate scope choice for a single-operator
  home-lab tool, not an oversight; don't add a dependency on a public CA or
  ACME without discussing it first, since it'd change what this tool assumes
  about network reachability (a public CA needs to validate domain
  ownership, which doesn't fit "never leaves your own machines").
- SSH between the coordinator and remote systems (non-interactive key auth,
  `~/.ssh/config` aliases) is a local environment setup decision the user
  makes themselves - ModelManager documents that it's needed but doesn't
  configure, scope, or restrict it. Don't add code that assumes a particular
  key-management scheme.
