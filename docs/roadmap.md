# Roadmap: positioning, tooling, and security cleanup

Captured from a persona review (DHH, 0xSero, and a newcomer-to-local-models
perspective) after the cross-host sharing feature landed. Six concrete asks
came out of that review; this groups them into phases ordered so each one is
self-contained and testable before moving to the next, and so later phases
(docs) can describe the final shape of earlier ones (positioning, cli,
Panel.qml) instead of chasing a moving target.

Work through phases in order. Each phase ends with its own resolve/test step
- don't start the next phase until the current one's test step passes.

## Phase 1 - Reposition the README (DHH #1, Newcomer #2) - DONE 2026-10-19

The project reads today like a personal single-user tool that grew a
multi-tenant feature bolted on. It's actually a model manager whose
sum-of-the-parts pitch - share GPU capacity with other machines on your
Tailscale/LAN, each person still manages their own models locally - is a
first-class feature, not an afterthought.

- [x] Rewrote the README's opening description/pitch to lead with: manage
      local models on your own machine, *and* optionally share or borrow
      GPU capacity with other systems you trust, over Tailscale/LAN.
- [x] Added a bullet to the README's "What it is / isn't" list explicitly
      calling out solo vs. shared usage, so a skimmer sees both halves
      without reading the full intro.
- [x] Updated `pyproject.toml`'s `description` and `manifest.json`'s
      top-level `description` to mention sharing with trusted peers, not
      just "your GPU machines." (`marketplace-thoughts.md` is an internal
      planning scratch doc, not user-facing listing copy - left alone.)

**Resolve/test:** re-read the README fresh, as if you'd never seen the repo -
confirms it answers "can I share this with a friend?" in the first few
lines without needing to hunt for the grants/hosts docs.

## Phase 2 - `cli.py` onto a real argument-parsing library (DHH #2)

1000+ lines of hand-rolled `argparse` wiring in `modelmanager/cli.py`. Move
it onto a proper library (Click or Typer - pick one, Typer gives type-hint-
driven parsing which fits this codebase's style better) instead of continuing
to hand-build subcommand dispatch.

- [ ] Pick Click vs Typer and note the choice (and why) in `CONTRIBUTING.md`.
- [ ] Migrate command-by-command (`auth`, `grant`, `hosts`, `stats`, model/
      system/engine/gpu_group/group CRUD, `ensure`/`stop`/`logs`/`log-path`,
      etc.), preserving every existing flag name and behavior exactly -
      this is a mechanical port, not a redesign.
- [ ] Keep the existing local-file-only bypass pattern for `grant`/`hosts`/
      `auth` (they don't go through `_client()`) - don't let the new
      library's conventions quietly change that.
- [ ] Update `docs/cli-reference.md` if any help text/flag presentation
      changes as a side effect of the new library.

**Resolve/test:** full `pytest` pass, plus a manual run-through of every
subcommand in `docs/cli-reference.md` against a scratch coordinator,
confirming output shape (especially JSON output used by Panel.qml's
`Process` calls) is byte-for-byte unchanged where the panel depends on it.

## Phase 3 - Split `Panel.qml` into modules (DHH #3)

2200+ lines in one file. Pull it apart into focused QML components (e.g.
host-targeting row + trust-confirm flow, models list/filter, stats section,
the various `Process` definitions grouped by concern) under a `panel/` or
`components/` subdirectory, with `Panel.qml` itself becoming a thin
composition root.

- [ ] Inventory current top-level sections/responsibilities in `Panel.qml`
      before splitting (host targeting, model list, group actions, stats,
      setup/skill-link panel, logs/terminal launching) to decide module
      boundaries up front rather than splitting ad hoc.
- [ ] Extract one module at a time, keeping the app working after each
      extraction (not one big-bang rewrite).
- [ ] Preserve every `pendingHost`/`pendingName`/`pendingSystem` stamping
      convention exactly - these are load-bearing for the stale-response
      fixes already shipped this project, not incidental structure.

**Resolve/test:** after each extraction, `omarchy-restart-shell` and
click through the affected area live; at the end, a full click-through of
every panel feature (local models, remote host switch + trust prompt,
group ensure/stop, stats, logs) exactly as done when each feature was
originally verified.

## Phase 4 - Security/risk surface pass (0xSero #1)

Audit, don't redesign - the goal is tightening what exists, not adding new
abstraction. Specifically:

- [ ] **Token mechanics**: confirm bearer tokens are high-entropy/random at
      generation (not user-chosen), document that assumption explicitly
      since `hash_token()` uses plain SHA-256 (fine for high-entropy secrets,
      wrong assumption otherwise).
- [ ] **Grant recon surface**: verify a scoped grant genuinely cannot learn
      anything (system names, model names, error-message detail) about
      systems outside its `allowed_systems`, including via `/v1/systems/
      stats`, error responses, and timing differences.
- [ ] **SSRF on `--peer`/`--host` resolution**: review `known_hosts.py` +
      `cli.py`'s host resolution path for whether a malicious/compromised
      peer entry (or probe target) could be pointed at `127.0.0.1`/internal
      services in a way that matters here - decide and document the actual
      threat model (this is a trusted-peer tool, not a public service) rather
      than adding defensive complexity against threats that don't apply.
- [ ] **TLS trust revocation**: confirm `hosts remove` actually invalidates a
      previously-pinned fingerprint (no stale trust left behind), and that
      there's a documented path for "I think this host's cert was
      compromised, what do I do."
- [ ] Write findings + decisions (fixed vs. accepted-and-documented) into
      `CONTRIBUTING.md`'s Security section so they're not re-litigated later.

**Resolve/test:** `pytest` pass; manual verification of any fix against a
real scoped grant + a real `--peer` connection (same rigor as the original
cross-host testing against airy), not just unit tests.

## Phase 5 - Docs and `skills.md` coverage for newcomers (Newcomer #1, ties back to DHH #1)

Once positioning (Phase 1), the CLI surface (Phase 2), and the security
story (Phase 4) are settled, write them up so a newcomer's own coding agent
can answer "can I share my GPU with a friend" / "how do I let someone else
use my models" without the human needing to read source.

- [ ] Add/expand a `skills.md` (or update the existing skill content under
      `skills/`) that explicitly covers: solo usage, and the grant/hosts
      sharing flow end-to-end, in plain Q&A-friendly language.
- [ ] Cross-link from the README's repositioned pitch (Phase 1) into this
      doc so both halves of the story ("manage your own" / "share with
      others") point at concrete how-to docs.
- [ ] Have a fresh agent (or re-read cold yourself) try to answer "how do I
      grant my friend access to one of my systems" using only the docs/
      skill content, with no other repo context, as the actual test.

**Resolve/test:** the cold-read exercise above - if it can't answer the
question correctly from docs alone, the docs aren't done.
