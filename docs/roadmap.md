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

## Phase 2 - `cli.py` onto a real argument-parsing library (DHH #2) - DONE 2026-10-19

1000+ lines of hand-rolled `argparse` wiring in `modelmanager/cli.py`. Moved
it onto Click.

- [x] Picked Click over Typer - already a transitive dependency (via
      `uvicorn`), no type-hint-driven magic to get wrong, and its explicit
      decorator style matches this codebase's existing style better. Noted
      in `CONTRIBUTING.md`.
- [x] Migrated every command (`auth`, `grant`, `hosts`, `stats`, model/
      system/engine/gpu_group/group CRUD, `ensure`/`stop`/`logs`/`log-path`,
      etc.) - same flag names, same `cmd_*` functions untouched, each Click
      command just builds the same kind of `SimpleNamespace` argparse used
      to hand them and calls through.
- [x] Kept the local-file-only bypass pattern for `grant`/`hosts`/`auth`
      (still never go through `_client()`).
- [x] `docs/cli-reference.md` needed no changes - every flag/positional
      shape is identical; only `--help`'s own formatting changed (Click's
      own style), which isn't documented there.
- [x] Added `click>=8.1` as an explicit dependency (`pyproject.toml`) and
      regenerated `requirements.lock` - a one-line diff (click gained a
      direct `via` entry, no version changes), verified installable with
      `--require-hashes` in a clean venv.

**Resolve/test:** full `pytest` pass (249 passed); manual run-through of
`serve`, `list`/`systems`/`groups`/`config`/`doctor`/`stats`/`version`,
`ensure`/`stop`/`logs`/`log-path` against missing models (clean 404 JSON),
`put` (both a validation error and a success), `grant add/allow/list/revoke/
remove` against a scratch registry, `hosts probe/add --yes/list/touch/
remove`, and `--peer <name>` actually reaching the scratch coordinator -
all against a disposable scratch coordinator/registry, never the real one.
Also confirmed the coordinator-unreachable path still prints the same clean
JSON error line (hit it by accident via a cert mismatch in the test setup).

## Phase 3 - Split `Panel.qml` into modules (DHH #3) - MOSTLY DONE 2026-10-19

2200+ lines in one file. Inventoried it first: lines 1-1036 are pure state/
coordination (root properties, functions, Timers - no visual tree, nothing
cleanly separable without threading the whole state surface through
multiple files for little benefit), lines 1037-1380 were 20 `Process`
blocks, and the rest was the visual tree.

- [x] Inventoried top-level sections before splitting (see above) - decided
      the state/coordination layer stays in Panel.qml as the legitimate
      "model" of this structure; Process definitions and self-contained
      visual chunks move out.
- [x] Extracted all 20 `Process` blocks into `procs/` (`DataProcs.qml`,
      `ActionProcs.qml`, `LogProcs.qml`, `HostProcs.qml`, `SetupProcs.qml`),
      grouped by concern, each taking `required property var root`.
      Instantiated in Panel.qml under their original ids via
      `property alias xProc: group.xProc`, so every existing
      `xProc.running`/`.pendingHost` call site in Panel.qml's own functions
      kept working with zero changes.
- [x] Extracted 4 self-contained visual chunks into `components/`
      (`HostTrustBlock.qml`, `ModelsList.qml`, `BottomPanel.qml`,
      `Toast.qml`) - each takes `root` and reads `root.*`/`root.xProc.*`;
      positioning (anchors/visible) stays at the instantiation site in
      Panel.qml since it depends on sibling ids that live there.
- [x] Preserved every `pendingHost`/`pendingName` stamping convention
      exactly - unchanged inside the extracted Process files.
- [x] One small addition needed: `procs/HostProcs.qml`'s probe/add failure
      paths used to poke `hostField.text` directly; added `root.
      resetHostField()` so they don't need bare access to a TextField that
      now lives in Panel.qml's own (not-yet-extracted) visual tree.
- [ ] **Deferred, not abandoned**: `hostRow` (Coordinator field),
      `hostLoadingRow`, and `modelsHeaderRow` are still inline in Panel.qml.
      They're smaller, and `hostRow`'s `hostField` and `modelsHeaderRow`'s
      `modelFilterField` are both read by `keyCatcher.blocked` - extracting
      them cleanly needs an exposed alias (e.g. `property alias
      fieldActiveFocus: hostField.activeFocus`) plus updating every
      external call site that pokes `hostField.text` (`switchHost()`,
      `rejectHostTrust()`). Worth doing as a follow-up, not worth rushing
      in the same pass as the lower-risk extractions above.

Net result: `Panel.qml` 2225 -> 1311 lines; 9 new files (procs/ + 
components/) totaling ~1100 lines, each with a header comment explaining
its scope.

**Resolve/test:** `pytest` (249 passed, unrelated to QML but confirms no
Python-side regression); `omarchy-restart-shell` after each extraction
step, checking `quickshell log -p /usr/share/omarchy/shell` for binding-loop/
type errors each time (none at any step); grepped every new file for stray
bare references to a Process id or visual id that moved elsewhere (none
found - every cross-reference correctly goes through `root.`); toggled the
live panel via `omarchy-shell shell toggle slideout.modelmanager` against
the real production coordinator on this machine and confirmed via its
access-log that `/v1/systems`, `/v1/config`, `/v1/models`, `/v1/groups`,
and `/v1/systems/stats` all actually fired and returned 200 immediately
after - i.e. the extracted DataProcs/ModelsList/BottomPanel wiring is
exercised end-to-end against real data, not just "no parse error." Did not
click an actual Ensure/Stop button against the real production coordinator
(that would act on real running models) - **a human click-through of the
panel (open it, switch hosts, Ensure/Stop a model, check Stats/Logs) is
still the one verification step only the user can do.**

## Phase 4 - Security/risk surface pass (0xSero #1) - DONE 2026-10-19

Audit, don't redesign - the goal was tightening what exists, not adding new
abstraction.

- [x] **Token mechanics**: confirmed `auth.generate_token()` is
      `secrets.token_hex(32)` (256 bits, CSPRNG) - the owner token and every
      grant token (generated the same way, on the grantee's own machine) are
      both high-entropy by construction. Documented the assumption directly
      on `grants.hash_token()`'s docstring (plain SHA-256 is only correct
      because of it). Considered a runtime minimum-length check to catch a
      typed-password mistake; reverted it - it broke 11 tests relying on
      short literal stand-in tokens, for a case the documented workflow
      already doesn't produce. Documented that decision instead (see
      `CONTRIBUTING.md`).
- [x] **Grant recon surface**: found and fixed a real leak -
      `routes._check_system_access`'s 403 message named the exact system(s)
      a model/engine/gpu_group actually lives on, which a scoped grant could
      read for any model name it could guess or enumerate, even with zero
      access to that system. Fixed by never echoing the checked systems back
      in the message. Added a regression test
      (`test_grant_filters_dump_all_endpoints_and_blocks_model_actions_outside_scope`)
      asserting the backing system name never appears in a 403 body.
      Group-action per-member forbidden results (`manager.py`'s
      `ensure_group`/`stop_group`/etc.) already used a generic message with
      no leak - no change needed there.
- [x] **SSRF on `--peer`/`hosts` resolution**: reviewed `known_hosts.py` and
      `cli.py`'s probe/add path - concluded this isn't SSRF in the sense
      that term usually means, since there's no REST route that takes a URL
      from a network caller and fetches it on the coordinator's behalf;
      `hosts probe`/`add` are local-only CLI commands run by the machine's
      own owner, same trust level as running `curl` themselves. Documented
      the reasoning in `CONTRIBUTING.md` rather than adding allowlisting
      against an attacker that doesn't exist here.
- [x] **TLS trust revocation**: confirmed `known_hosts.remove_known_host()`
      already deletes both the JSON entry and the cert PEM file - no stale
      trust left behind, `--peer <name>` fails cleanly afterward. No code
      change needed. Added a "compromised cert" recovery walkthrough to
      `docs/deployment.md` (remove, then re-add - `hosts add` always
      re-fetches and re-shows the fingerprint for confirmation, even for an
      already-known name, so a changed cert is never silently re-trusted).
- [x] Wrote findings + decisions (fixed vs. accepted-and-documented,
      including the timing-side-channel non-concern for this threat model)
      into `CONTRIBUTING.md`'s Security section.

**Resolve/test:** `pytest` (249 passed, including the new regression test);
confirmed the fix and the revert-of-the-token-check by re-running the full
suite after each change.

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
