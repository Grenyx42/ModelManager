# Roadmap: positioning, tooling, and security cleanup

Six items came out of a cross-persona review (DHH, 0xSero, and a
newcomer-to-local-models perspective) after the cross-host sharing feature
landed. Grouped into phases so each is self-contained before the next one
starts.

## Phase 1 - Reposition the README - done

- README's opening now leads with both solo and shared usage (Tailscale/LAN),
  instead of reading like a single-user tool with sharing bolted on.
- Added an explicit solo-vs-shared bullet to "What it is / isn't".
- `pyproject.toml` and `manifest.json` descriptions mention sharing with
  trusted peers, not just "your GPU machines."

## Phase 2 - `cli.py` onto Click - done

- Migrated ~1000 lines of hand-rolled `argparse` to Click (already a
  transitive dependency via `uvicorn`) - same flags and behavior for every
  command, `cmd_*` functions untouched.
- Added `click>=8.1` to `pyproject.toml` and regenerated `requirements.lock`.
- Verified: full test suite green, plus a manual pass over every command
  against a scratch registry/coordinator.

## Phase 3 - Split `Panel.qml` into modules - mostly done

- Extracted the 20 `Process` blocks into `procs/` and 4 self-contained
  visual chunks into `components/`; the state/coordination layer stays in
  `Panel.qml`, since threading it through multiple files wasn't worth it.
- Net: `Panel.qml` 2225 -> 1311 lines; 9 new files (~1100 lines total).
- Deferred, not abandoned: `hostRow`, `hostLoadingRow`, and
  `modelsHeaderRow` stay inline - extracting them cleanly needs new
  exposed aliases for fields `keyCatcher.blocked` reads directly.
- Verified: full test suite green, clean `omarchy-restart-shell` reload
  with no QML errors, and real coordinator requests confirmed firing
  end-to-end against the live panel after the refactor.

## Phase 4 - Security/risk surface pass - done

- Fixed a real info leak: a scoped grant's 403 response named the exact
  system a model lived on, letting a caller enumerate systems it had no
  access to just by guessing model names. Fixed; added a regression test.
- Reviewed and found no issues: token entropy (`secrets.token_hex(32)`,
  256-bit CSPRNG), `--peer`/`hosts` resolution (CLI-only, run by the
  machine's own owner - not attacker-reachable, not SSRF), and TLS trust
  revocation (`known_hosts.remove_known_host()` already clears both the
  JSON entry and the cert file cleanly).
- Findings and the accepted-as-is decisions are written up in
  `CONTRIBUTING.md`'s Security section.

## Phase 5 - Docs and skills coverage for newcomers - not started

- Expand `skills/` content to cover solo usage and the grant/hosts sharing
  flow end-to-end, in plain Q&A-friendly language.
- Cross-link from the README's repositioned pitch (Phase 1) into that doc.
- Test: a newcomer's coding agent should be able to answer "how do I share
  my GPU with a friend" using only the docs/skill content, no other repo
  context.
