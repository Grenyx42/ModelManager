#!/usr/bin/env python3
"""Tier 2/3 smoke test: exercise a real model against whatever coordinator
is actually reachable (this machine's own install, e.g. airy's local Ollama
model - or a peer, e.g. sleek's nomic-embed over `--peer`).

Deliberately talks to the live, already-running coordinator through the real
`modelmanager` CLI (not the REST API directly, not mocks) - the point is to
catch exactly the class of bug unit tests structurally can't: real hardware,
real model weights, real GPU/CPU stats reporting (this is how the CPU/RAM
stats bug on airy was first found - a clean venv install can't reproduce
host-specific hardware reporting, only a live box can).

Read-only by default (list/stats/logs, plus verify only when the model is
already active - never stops anything, never loads/evicts anything). Pass
--allow-stop-start to also exercise the stop/ensure lifecycle (and to verify
an inactive model, which has to load it first), for a model you're fine
hammering regardless of its state before or after (e.g. sleek's
nomic-embed) - never on a model you're not sure is idle.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


def run_cli(global_args: list[str], *args: str, timeout: float = 60) -> tuple[int, str]:
    cmd = ["modelmanager", *global_args, *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    return result.returncode, (result.stdout or result.stderr).strip()


def run_json(global_args: list[str], *args: str, timeout: float = 60) -> tuple[int, object]:
    rc, out = run_cli(global_args, *args, timeout=timeout)
    try:
        return rc, json.loads(out)
    except json.JSONDecodeError:
        return rc, out


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        self.results.append((label, ok, detail))
        status = "OK  " if ok else "FAIL"
        print(f"  {status}  {label}" + (f"  ({detail})" if detail else ""))
        return ok

    def summary(self) -> bool:
        passed = sum(1 for _, ok, _ in self.results if ok)
        failed = len(self.results) - passed
        print(f"\n{passed} passed, {failed} failed")
        return failed == 0


def find_model(models: object, name: str) -> dict | None:
    if not isinstance(models, list):
        return None
    for m in models:
        if m.get("name") == name:
            return m
    return None


def check_stats(report: Report, global_args: list[str], system: str | None) -> None:
    args = ["stats"] + ([system] if system else [])
    rc, body = run_json(global_args, *args)
    if not report.check("stats: command succeeds", rc == 0, f"rc={rc}"):
        return
    # `stats <system>` returns one {cpu, gpus} object; `stats` with no
    # argument returns {"results": [{system, ok, cpu, gpus, detail}, ...]}
    # for every reachable system in one call - two different shapes for the
    # same underlying data.
    if isinstance(body, dict) and "results" in body:
        entries = body["results"]
    elif isinstance(body, dict) and "cpu" in body:
        entries = [body]
    else:
        report.check("stats: response has the expected shape", False, f"got {body!r}")
        return
    if not entries:
        report.check("stats: at least one system reported", False, f"got {body!r}")
        return
    for entry in entries:
        if not isinstance(entry, dict):
            report.check("stats: entry has the expected shape", False, f"got {entry!r}")
            continue
        if entry.get("ok") is False:
            report.check(f"stats: system {entry.get('system', '?')} reachable", False, entry.get("detail") or "unreachable")
            continue
        cpu = entry.get("cpu") or {}
        util = cpu.get("utilization_pct")
        used = cpu.get("memory_used_mb")
        total = cpu.get("memory_total_mb")
        report.check(
            "stats: cpu.utilization_pct is a sane percentage",
            isinstance(util, (int, float)) and 0 <= util <= 100,
            f"got {util!r}",
        )
        report.check(
            "stats: 0 <= cpu memory_used_mb <= memory_total_mb (and > 0)",
            isinstance(used, (int, float)) and isinstance(total, (int, float)) and 0 < total and 0 <= used <= total,
            f"used={used!r} total={total!r}",
        )
        for gpu in entry.get("gpus") or []:
            g_used, g_total = gpu.get("memory_used_mb"), gpu.get("memory_total_mb")
            report.check(
                f"stats: 0 <= gpu[{gpu.get('index')}] memory_used_mb <= memory_total_mb (and > 0)",
                isinstance(g_used, (int, float)) and isinstance(g_total, (int, float)) and 0 < g_total and 0 <= g_used <= g_total,
                f"used={g_used!r} total={g_total!r}",
            )


def wait_for_active(global_args: list[str], model: str, want_active: bool, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, body = run_json(global_args, "list")
        if rc == 0:
            entry = find_model(body, model)
            if entry is not None and bool(entry.get("active")) == want_active:
                return True
        time.sleep(1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="model name to exercise, as `modelmanager list` names it")
    parser.add_argument("--system", default=None, help="system to pull stats from (defaults to every reachable system)")
    parser.add_argument("--peer", default=None, help="target a remote coordinator already trusted via `modelmanager hosts add`")
    parser.add_argument("--url", default=None, help="coordinator base URL (alternative to --peer)")
    parser.add_argument("--verify-timeout", type=float, default=30.0)
    parser.add_argument(
        "--allow-stop-start", action="store_true",
        help="also exercise stop + re-ensure on this model - only pass this for a model you're fine "
             "disrupting regardless of its state before/after (e.g. an idle, unused embedding model)",
    )
    args = parser.parse_args()

    # This talks to whatever `modelmanager` CLI is already installed and
    # running, not this working copy - a code change here needs a
    # redeploy (scripts/setup.sh, or the `pip install --force-reinstall` +
    # `systemctl --user restart` this session used directly) before this
    # tier actually exercises it, or it silently tests the previous
    # version.
    #
    # This used to skip the check entirely for --peer/--url, on the
    # reasoning that a remote target's own coordinator
    # is a different machine's install this box can't speak to the
    # freshness of - true, but every call below still runs the *local*
    # `modelmanager` binary (it just appends --peer/--url), so that local
    # CLI's own freshness still matters even then. Only the "is a local
    # coordinator process running this code" half is actually irrelevant
    # for a remote target - skip just that half, not the whole check.
    # --peer is always a different trusted host, but --url isn't
    # necessarily remote - `--url https://127.0.0.1:...`
    # still selects *this* box's own coordinator, so the process check is
    # still meaningful there; only skip it for a genuinely non-loopback url.
    #
    # Found by Claude Code's review: a schemeless --url (e.g.
    # "127.0.0.1:8778") makes urlparse().hostname None, which isn't in
    # the loopback tuple either - treating "couldn't parse it" the same
    # as "confirmed remote" would skip the freshness check on exactly the
    # malformed input it's least safe to assume anything about. Only
    # mark it remote when parsing actually produced a non-loopback host.
    url_host = urlparse(args.url).hostname if args.url else None
    is_remote = bool(args.peer) or (url_host is not None and url_host not in ("127.0.0.1", "localhost", "::1"))
    check_script = Path(__file__).with_name("check_fresh_install.py")
    check_args = [sys.executable, str(check_script)]
    if is_remote:
        check_args.append("--skip-process-check")
    # check_fresh_install.py's own
    # _effective_local_url() only reads MODELMANAGER_URL/MODELMANAGER_PORT
    # from its environment - with no --url of our own, it has no way to
    # know this script's own target is `args.url`, so it was checking
    # whatever the ambient env (or the 8778 default) resolves to while
    # every actual call below hits `args.url` instead. A loopback --url on
    # a *different* port than the ambient default could otherwise validate
    # the wrong coordinator entirely - exactly the false-OK this exists to
    # prevent.
    check_env = {**os.environ, "MODELMANAGER_URL": args.url} if args.url else None
    result = subprocess.run(check_args, capture_output=True, text=True, env=check_env)
    print(result.stdout.strip() or result.stderr.strip())
    if result.returncode != 0:
        return 1

    global_args: list[str] = []
    if args.peer:
        global_args += ["--peer", args.peer]
    if args.url:
        global_args += ["--url", args.url]

    report = Report()
    target = args.peer or args.url or "local coordinator"
    print(f"== tier 2/3: hardware smoke test - model={args.model!r} target={target} ==")

    rc, _ = run_cli(global_args, "version")
    if not report.check("coordinator reachable (version)", rc == 0):
        return 1 if not report.summary() else 0

    rc, models = run_json(global_args, "list")
    report.check("list: command succeeds", rc == 0, f"rc={rc}")
    entry = find_model(models, args.model) if rc == 0 else None
    if not report.check(f"list: model {args.model!r} is a known definition", entry is not None):
        report.summary()
        return 1
    was_active = bool(entry.get("active"))

    check_stats(report, global_args, args.system)

    # `verify` always calls Manager.ensure() first (coordinator/manager.py) -
    # on an already-active model that's a no-op, but on an inactive one it
    # can load it and evict whatever else currently occupies its gpu_group.
    # Calling it unconditionally here broke this script's own "read-only
    # by default" promise. Only call it when it can't disrupt anything
    # else: the model was already active, or the caller explicitly
    # accepted disruption via --allow-stop-start.
    #
    # `was_active` was read once, well before
    # this point (check_stats runs in between) - if something else (e.g.
    # the idle sweep) stopped the model in that window, this would still
    # trust the stale `True` and call verify anyway. Re-checking right
    # before the gate can't close the race entirely (ensure() itself still
    # runs a moment later), but it shrinks the window from "this whole
    # function so far" down to one `list` call's worth.
    if not args.allow_stop_start:
        # A failed recheck (or one that doesn't return this model) used
        # to leave `was_active` at its earlier, possibly-stale value -
        # which could still be `True`,
        # letting verify's ensure() load/evict on nothing more than a
        # guess. Derive it only from a recheck that actually succeeded and
        # found the model; anything else means "not confirmed active",
        # which is the only direction it's safe to be wrong in here.
        rc, recheck_models = run_json(global_args, "list")
        recheck_entry = find_model(recheck_models, args.model) if rc == 0 else None
        was_active = bool(recheck_entry.get("active")) if recheck_entry is not None else False

    if was_active or args.allow_stop_start:
        rc, verify_body = run_json(global_args, "verify", args.model, "--timeout", str(args.verify_timeout),
                                    timeout=args.verify_timeout + 10)
        report.check("verify: command succeeds", rc == 0, f"rc={rc}")
        if not isinstance(verify_body, dict):
            report.check("verify: response is valid JSON", False, f"got {verify_body!r}")
        else:
            report.check("verify: model answered ok", bool(verify_body.get("ok")), json.dumps(verify_body))
            latency = verify_body.get("latency_ms")
            report.check(
                "verify: latency_ms is a positive number",
                isinstance(latency, (int, float)) and latency > 0,
                f"got {latency!r}",
            )
    else:
        print("  SKIP  verify (model isn't active - would load it and could evict another model's "
              "gpu_group; pass --allow-stop-start to allow that)")

    rc, _ = run_cli(global_args, "logs", args.model, "--lines", "20")
    report.check("logs: command succeeds", rc == 0, f"rc={rc}")

    if args.allow_stop_start:
        try:
            rc, _ = run_cli(global_args, "stop", args.model, "--force")
            report.check("stop: command succeeds", rc == 0, f"rc={rc}")
            report.check("stop: model goes inactive", wait_for_active(global_args, args.model, want_active=False))

            rc, _ = run_cli(global_args, "ensure", args.model, timeout=120)
            report.check("ensure: command succeeds", rc == 0, f"rc={rc}")
            report.check(
                "ensure: model becomes active again",
                wait_for_active(global_args, args.model, want_active=True, timeout=60),
            )

            rc, verify_body = run_json(global_args, "verify", args.model, "--timeout", str(args.verify_timeout),
                                        timeout=args.verify_timeout + 10)
            report.check("verify after restart: model answers ok",
                          rc == 0 and isinstance(verify_body, dict) and bool(verify_body.get("ok")))
        finally:
            # Leave it loaded regardless of pass/fail above, if it was loaded
            # when we found it - a failed assertion mid-sequence must never
            # be the reason a standing dependency sits stopped afterward.
            if was_active:
                run_cli(global_args, "ensure", args.model, timeout=120)

    check_stats(report, global_args, args.system)

    return 0 if report.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
