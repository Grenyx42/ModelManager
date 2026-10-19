from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import click
import httpx
from pydantic import ValidationError

from . import auth, grants, known_hosts
from .config import default_tls_cert_path, default_tls_key_path, load_registry

DEFAULT_SERVE_HOST = os.environ.get("MODELMANAGER_HOST", "0.0.0.0")
DEFAULT_SERVE_PORT = int(os.environ.get("MODELMANAGER_PORT", "8778"))
# A local caller always talks to 127.0.0.1 regardless of what host the
# coordinator itself binds to (see --host above) - but the port has to
# match, so it falls back to MODELMANAGER_PORT (not MODELMANAGER_HOST) when
# MODELMANAGER_URL isn't set explicitly. Otherwise, changing just the port
# (e.g. via scripts/setup.sh's listen-address prompt, which only writes an
# EnvironmentFile the systemd-managed coordinator itself reads - nothing
# sources that file into this process) would silently break every local
# `modelmanager` call and the bar plugin with it.
DEFAULT_URL = os.environ.get("MODELMANAGER_URL", f"https://127.0.0.1:{DEFAULT_SERVE_PORT}")


def _client(args) -> httpx.Client:
    # --token wins outright; otherwise the same MODELMANAGER_TOKEN-then-file
    # resolution the coordinator itself uses (see auth.read_token) - so the
    # bar plugin, which only ever shells out to this CLI, authenticates for
    # free with no changes of its own, as long as it's running as the same
    # user that ran scripts/setup.sh. This same token is what gets presented
    # to a *remote* coordinator too (via --peer below) - whoever granted you
    # access already has a hash of this exact value (see grants.py), there's
    # no separate per-peer credential to manage.
    token = args.token or auth.read_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = args.url
    cert_path = default_tls_cert_path()
    peer = getattr(args, "peer", None)
    if peer:
        entry = known_hosts.get_known_host(peer)
        if entry is None:
            print(json.dumps({"error": f"unknown host {peer!r} - run `modelmanager hosts add {peer} <url>` first"}))
            sys.exit(1)
        url = entry["url"]
        cert_path = Path(entry["cert_file"])
    # The coordinator's cert is self-signed (scripts/setup.sh), so it isn't
    # in any system CA bundle - pass it explicitly as the thing to trust,
    # same as copying the token, rather than verify=False (which would
    # accept *any* cert and defeat the point of TLS). A caller that hasn't
    # been handed a copy of it yet (or hasn't `hosts add`-ed a --peer) falls
    # back to the system CA bundle, which will fail verification with a
    # clear TLS error pointing at the real problem instead of crashing on a
    # missing file.
    verify = str(cert_path) if cert_path.exists() else True
    return httpx.Client(base_url=url, timeout=30, headers=headers, verify=verify)


def _fetch_cert_fingerprint(url: str) -> tuple[str, str]:
    """Connects to `url`'s host:port and returns (pem, fingerprint) without
    verifying anything - this *is* the trust-on-first-connect probe, so by
    definition there's nothing yet to verify against. The fingerprint is
    SHA256 of the cert's DER bytes, colon-hex-formatted like `openssl
    x509 -fingerprint` / an SSH host key, for a human to visually confirm
    out-of-band before trusting it."""
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"not a valid URL: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pem = ssl.get_server_certificate((parsed.hostname, port))
    der = ssl.PEM_cert_to_DER_cert(pem)
    digest = hashlib.sha256(der).hexdigest().upper()
    fingerprint = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
    return pem, fingerprint


def _print_response(resp: httpx.Response) -> int:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text}
    if resp.status_code >= 400 and "error" not in body and "detail" in body:
        body = {"error": body["detail"]}
    print(json.dumps(body))
    return 0 if resp.status_code < 400 else 1


def _print_error_response(resp: httpx.Response) -> int:
    """Pretty-mode counterpart to _print_response() for an error response:
    renders the coordinator's {"error", "code"} body as a readable line
    instead of raw JSON. Falls back to _print_response() for anything that
    doesn't have that shape (e.g. a raw FastAPI request-validation 422)."""
    try:
        body = resp.json()
    except ValueError:
        return _print_response(resp)
    if "code" not in body or "error" not in body:
        return _print_response(resp)
    c = _make_colorizer()
    print(c(f"✗ [{body['code']}]", "red", "bold") + f" {body['error']}")
    return 1


def cmd_serve(args) -> int:
    import uvicorn

    cert_path = default_tls_cert_path()
    key_path = default_tls_key_path()
    if not cert_path.exists() or not key_path.exists():
        print(json.dumps({
            "error": f"no TLS cert/key at {cert_path} / {key_path} - run scripts/setup.sh to generate one "
                     "(or point MODELMANAGER_TLS_CERT_PATH/MODELMANAGER_TLS_KEY_PATH at your own)",
            "code": "TLS_NOT_CONFIGURED",
        }))
        return 1

    uvicorn.run(
        "modelmanager.coordinator.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=False,  # replaced by our own middleware -> access.log (timestamps, duration)
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )
    return 0


_COLOR = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "magenta": "\033[35m",
    "gray": "\033[90m",
    "red": "\033[31m",
}


def _make_colorizer():
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def c(text: str, *codes: str) -> str:
        if not use_color or not codes:
            return text
        return "".join(_COLOR[code] for code in codes) + text + _COLOR["reset"]

    return c


def _human_idle(seconds) -> str:
    if seconds is None:
        return "never used"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _print_models_pretty(
    models: list[dict], gpu_group_system: dict[str, str], model_groups: dict[str, list[str]] | None = None
) -> None:
    c = _make_colorizer()

    by_system: dict[str, list[dict]] = {}
    for model in models:
        gpu_groups = model.get("gpu_groups") or []
        system = gpu_group_system.get(gpu_groups[0]) if gpu_groups else None
        candidate = model.get("candidate")
        if candidate and candidate.get("host"):
            system = candidate["host"]
        by_system.setdefault(system or "unknown", []).append(model)

    for system in sorted(by_system):
        print(c(system, "bold", "cyan"))
        for model in sorted(by_system[system], key=lambda m: m["name"]):
            active = model.get("active")
            bullet = c("●", "green") if active else c("○", "gray")
            name = c(model["name"], "bold")
            tags = []
            if model.get("pinned"):
                tags.append(c("pinned", "magenta"))
            if model.get("in_use"):
                tags.append(c("in-use", "yellow"))
            if active and model.get("candidate"):
                candidate = model["candidate"]
                tags.append(c(candidate["engine"], "dim"))
                tags.append(c(f"{candidate['host']}:{candidate['port']}", "dim"))
            else:
                timeout = model.get("idle_timeout_seconds")
                tags.append(c(f"idle_timeout={timeout}s" if timeout else "no idle timeout", "dim"))
            tags.append(c(_human_idle(model.get("idle_seconds")), "dim"))
            print(f"  {bullet} {name}  " + "  ".join(tags))
        print()

    if not model_groups:
        return
    by_name = {m["name"]: m for m in models}
    print(c("groups", "bold", "cyan"))
    for group_name in sorted(model_groups):
        members = model_groups[group_name]
        active_count = sum(1 for m in members if by_name.get(m, {}).get("active"))
        print(f"  {c(group_name, 'bold')}  " + c(f"{active_count}/{len(members)} active", "dim"))
        for member in members:
            info = by_name.get(member)
            if info is None:
                print(f"    {c('?', 'yellow')} {member}  " + c("not in registry", "yellow"))
                continue
            bullet = c("●", "green") if info.get("active") else c("○", "gray")
            host = (info.get("candidate") or {}).get("host")
            tag = c(host, "dim") if host else ""
            print(f"    {bullet} {member}  {tag}")
    print()


def cmd_list(args) -> int:
    with _client(args) as client:
        resp = client.get("/v1/models")
        if not args.pretty:
            return _print_response(resp)
        if resp.status_code >= 400:
            return _print_error_response(resp)
        config_resp = client.get("/v1/config")
        gpu_group_system = {}
        model_groups = {}
        if config_resp.status_code < 400:
            config = config_resp.json()
            gpu_group_system = {name: info.get("system") for name, info in config.get("gpu_groups", {}).items()}
            model_groups = config.get("groups", {})
        _print_models_pretty(resp.json(), gpu_group_system, model_groups)
        return 0


def _post_model_or_group(client: httpx.Client, name: str, action: str, params: dict | None = None) -> httpx.Response:
    """POST .../models/{name}/{action}, falling back to .../groups/{name}/{action}
    when name isn't a known model - so `ensure`/`stop` work unchanged for a
    single model but also accept a group name without a separate command."""
    resp = client.post(f"/v1/models/{name}/{action}", params=params or {})
    if resp.status_code == 404:
        group_resp = client.post(f"/v1/groups/{name}/{action}", params=params or {})
        if group_resp.status_code != 404:
            return group_resp
    return resp


def _get_system_or_group(client: httpx.Client, name: str, suffix: str) -> httpx.Response:
    """GET .../systems/{name}{suffix}, falling back to .../groups/{name}{suffix}
    when name isn't a known system - so `stats` accepts a group name and
    reports stats for whichever system(s) its members actually run on."""
    resp = client.get(f"/v1/systems/{name}{suffix}")
    if resp.status_code == 404:
        group_resp = client.get(f"/v1/groups/{name}{suffix}")
        if group_resp.status_code != 404:
            return group_resp
    return resp


def _dry_run(client: httpx.Client, name: str) -> httpx.Response:
    """GET .../models/{name}/dry-run, falling back to .../groups/{name}/dry-run
    when name isn't a known model - same fallback shape as _post_model_or_group."""
    resp = client.get(f"/v1/models/{name}/dry-run")
    if resp.status_code == 404:
        group_resp = client.get(f"/v1/groups/{name}/dry-run")
        if group_resp.status_code != 404:
            return group_resp
    return resp


def _print_dry_run_result_pretty(result: dict, c, indent: str = "  ") -> None:
    action = result.get("action")
    label = {
        "keep": c("KEEP", "green"),
        "start": c("START", "cyan"),
        "swap": c("SWAP", "yellow"),
        "blocked": c("BLOCKED", "red"),
        "error": c("ERROR", "red"),
    }.get(action, action or "?")
    name = c(result.get("model", "?"), "bold")
    tags = []
    if result.get("host") and result.get("port"):
        tags.append(c(f"{result['host']}:{result['port']}", "dim"))
    if result.get("evicts"):
        tags.append(c(f"evicts {result['evicts']}", "yellow"))
    print(f"{indent}{label}  {name}  " + "  ".join(tags))
    if result.get("blocked_reason"):
        print(c(f"{indent}    {result['blocked_reason']}", "red"))
    if result.get("detail"):
        print(c(f"{indent}    {result['detail']}", "red"))
    for path in result.get("missing_paths") or []:
        print(c(f"{indent}    missing: {path}", "red"))
    for dep in result.get("depends_on") or []:
        print(c(f"{indent}  depends on:", "dim"))
        _print_dry_run_result_pretty(dep, c, indent=indent + "    ")


def _print_dry_run_pretty(body: dict) -> None:
    c = _make_colorizer()
    if "results" in body:
        print(c(body.get("group", "group"), "bold", "cyan"))
        for result in body["results"]:
            _print_dry_run_result_pretty(result, c)
    else:
        _print_dry_run_result_pretty(body, c)


def cmd_ensure(args) -> int:
    with _client(args) as client:
        if args.dry_run:
            resp = _dry_run(client, args.name)
            if not args.pretty:
                return _print_response(resp)
            if resp.status_code >= 400:
                return _print_error_response(resp)
            _print_dry_run_pretty(resp.json())
            return 0
        return _print_response(_post_model_or_group(client, args.name, "ensure"))


def _print_verify_result_pretty(result: dict, c) -> None:
    ok = result.get("ok")
    bullet = c("?", "gray") if ok is None else (c("✓", "green") if ok else c("✗", "red"))
    name = c(result["name"], "bold")
    tags = []
    if result.get("engine"):
        tags.append(c(result["engine"], "dim"))
    if result.get("host") and result.get("port"):
        tags.append(c(f"{result['host']}:{result['port']}", "dim"))
    if result.get("latency_ms") is not None:
        tags.append(c(f"{result['latency_ms']:.0f}ms", "dim"))
    print(f"  {bullet} {name}  " + "  ".join(tags))
    if result.get("detail"):
        print(c(f"      {result['detail']}", "yellow" if ok is False else "dim"))


def _print_verify_pretty(body: dict) -> None:
    c = _make_colorizer()
    if "results" in body:
        print(c(body.get("group", "group"), "bold", "cyan"))
        for result in body["results"]:
            _print_verify_result_pretty(result, c)
    else:
        _print_verify_result_pretty(body, c)


def cmd_verify(args) -> int:
    with httpx.Client(base_url=args.url, timeout=args.timeout + 5) as client:
        resp = _post_model_or_group(client, args.name, "verify", {"timeout": args.timeout})
        if not args.pretty:
            return _print_response(resp)
        if resp.status_code >= 400:
            return _print_error_response(resp)
        _print_verify_pretty(resp.json())
        return 0


def cmd_acquire(args) -> int:
    params = {}
    if args.ttl is not None:
        params["ttl_seconds"] = args.ttl
    with _client(args) as client:
        return _print_response(client.post(f"/v1/models/{args.name}/acquire", params=params))


def cmd_release(args) -> int:
    params = {}
    if args.lease_id is not None:
        params["lease_id"] = args.lease_id
    with _client(args) as client:
        return _print_response(client.post(f"/v1/models/{args.name}/release", params=params))


def cmd_stop(args) -> int:
    params = {"force": "true"} if args.force else {}
    with _client(args) as client:
        return _print_response(_post_model_or_group(client, args.name, "stop", params))


def cmd_groups(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/groups"))


def cmd_group_ensure(args) -> int:
    with _client(args) as client:
        return _print_response(client.post(f"/v1/groups/{args.name}/ensure"))


def cmd_group_verify(args) -> int:
    with httpx.Client(base_url=args.url, timeout=args.timeout + 5) as client:
        resp = client.post(f"/v1/groups/{args.name}/verify", params={"timeout": args.timeout})
        if not args.pretty:
            return _print_response(resp)
        if resp.status_code >= 400:
            return _print_error_response(resp)
        _print_verify_pretty(resp.json())
        return 0


def cmd_group_stop(args) -> int:
    params = {"force": "true"} if args.force else {}
    with _client(args) as client:
        return _print_response(client.post(f"/v1/groups/{args.name}/stop", params=params))


def cmd_group_stats(args) -> int:
    with _client(args) as client:
        resp = client.get(f"/v1/groups/{args.name}/stats")
        if not args.pretty:
            return _print_response(resp)
        if resp.status_code >= 400:
            return _print_error_response(resp)
        _print_stats_pretty(resp.json())
        return 0


def _windows_tail_command(ssh_host: str, path: str, lines: int) -> list[str]:
    escaped = path.replace("'", "''")
    return [
        "ssh",
        ssh_host,
        "powershell",
        "-NoProfile",
        "-Command",
        f"Get-Content -Path '{escaped}' -Wait -Tail {lines}",
    ]


def _follow_command(info: dict, lines: int) -> list[str] | None:
    paths = info.get("paths") or []
    if not paths:
        return None
    if info.get("control") == "ssh" and info.get("os") == "windows":
        # Get-Content -Wait silently produces no output at all when given
        # more than one -Path (confirmed empirically) - single-path only here,
        # the multi-path case is handled by running one process per file.
        if len(paths) == 1:
            return _windows_tail_command(info["ssh_host"], paths[0], lines)
        return None
    if info.get("control") == "ssh":
        return ["ssh", info["ssh_host"], "tail", "-f", "-n", str(lines), *paths]
    return ["tail", "-f", "-n", str(lines), *paths]


def _follow_multi_windows(ssh_host: str, paths: list[str], lines: int) -> int:
    import subprocess
    import threading

    procs = [
        subprocess.Popen(
            _windows_tail_command(ssh_host, path, lines),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",  # a log is for reading, not re-encoding - one bad byte
            # over SSH (console codepage weirdness, a partial write, whatever)
            # shouldn't kill this thread and silently go quiet on one stream.
            bufsize=1,
        )
        for path in paths
    ]

    def pump(proc: subprocess.Popen, label: str) -> None:
        for line in proc.stdout:
            print(f"[{label}] {line}", end="")

    from pathlib import PureWindowsPath

    threads = [
        threading.Thread(target=pump, args=(proc, PureWindowsPath(path).name), daemon=True)
        for proc, path in zip(procs, paths)
    ]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in procs:
            proc.terminate()
    return 0


def cmd_logs(args) -> int:
    if not args.follow:
        with _client(args) as client:
            resp = client.get(f"/v1/models/{args.name}/logs", params={"lines": args.lines})
            if resp.status_code >= 400:
                return _print_response(resp)
            print(resp.json().get("logs", ""))
            return 0

    import subprocess

    with _client(args) as client:
        resp = client.get(f"/v1/models/{args.name}/log-path")
        if resp.status_code >= 400:
            return _print_response(resp)
        info = resp.json()

    paths = info.get("paths") or []
    if info.get("control") == "ssh" and info.get("os") == "windows" and len(paths) > 1:
        return _follow_multi_windows(info["ssh_host"], paths, args.lines)

    command = _follow_command(info, args.lines)
    if command is None:
        # No discrete log file (e.g. an externally-managed ollama service,
        # only in journalctl) - poll the same endpoint `logs` uses instead.
        command = ["watch", "-n", "2", sys.argv[0], "logs", args.name, "--lines", str(args.lines)]
    try:
        return subprocess.run(command).returncode
    except KeyboardInterrupt:
        return 0


def cmd_log_path(args) -> int:
    with _client(args) as client:
        return _print_response(client.get(f"/v1/models/{args.name}/log-path"))


def _print_gpus_pretty(gpus: list[dict], c, indent: str = "  ") -> None:
    for gpu in gpus:
        pct = gpu.get("utilization_pct")
        bar_color = "red" if pct is not None and pct >= 90 else ("yellow" if pct is not None and pct >= 50 else "green")
        print(
            f"{indent}[{gpu.get('index')}] {gpu.get('name')}  "
            + c(f"{gpu.get('memory_used_mb')}/{gpu.get('memory_total_mb')}MB", "dim")
            + "  " + c(f"{pct}% util", bar_color)
            + "  " + c(f"{gpu.get('temperature_c')}°C", "dim")
        )


def _print_stats_pretty(body: dict) -> None:
    c = _make_colorizer()
    if "results" not in body:
        _print_gpus_pretty(body.get("gpus", []), c)
        return
    if "group" in body:
        print(c(body["group"], "bold", "cyan"))
    for result in body["results"]:
        models = ", ".join(result.get("models", []))
        label = f"  {c(result['system'], 'bold')}"
        if models:
            label += "  " + c(f"({models})", "dim")
        print(label)
        if result.get("ok"):
            _print_gpus_pretty(result.get("gpus") or [], c, indent="    ")
        else:
            print(c(f"    {result.get('detail')}", "yellow"))


def cmd_stats(args) -> int:
    with _client(args) as client:
        # No system/group name -> every (allowed) system in one request
        # (GET /v1/systems/stats) instead of one call per system - this is
        # what the bar panel's stats sweep uses, so switching to a remote
        # peer doesn't pay a fresh TLS handshake per system in sequence.
        resp = client.get("/v1/systems/stats") if not args.system else _get_system_or_group(client, args.system, "/stats")
        if not args.pretty:
            return _print_response(resp)
        if resp.status_code >= 400:
            return _print_error_response(resp)
        _print_stats_pretty(resp.json())
        return 0


def cmd_access_log(args) -> int:
    with _client(args) as client:
        resp = client.get("/v1/access-log", params={"lines": args.lines})
        if resp.status_code >= 400:
            return _print_response(resp)
        print(resp.json().get("log", ""))
        return 0


def cmd_lifecycle_log(args) -> int:
    with _client(args) as client:
        resp = client.get("/v1/lifecycle-log", params={"lines": args.lines})
        if resp.status_code >= 400:
            return _print_response(resp)
        print(resp.json().get("log", ""))
        return 0


def cmd_systems(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/systems"))


def cmd_summary(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/status/summary"))


def cmd_help(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/help"))


def cmd_version(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/version"))


def _print_doctor_pretty(body: dict) -> None:
    c = _make_colorizer()
    print(c("systems", "bold", "cyan"))
    for system in body.get("systems", []):
        bullet = c("✓", "green") if system.get("ok") else c("✗", "red")
        tail = f"  {c(system['detail'], 'red')}" if system.get("detail") else ""
        print(f"  {bullet} {system['system']}{tail}")
    print()
    print(c("models", "bold", "cyan"))
    for model in body.get("models", []):
        _print_dry_run_result_pretty(model, c)
    print()
    print(c("OK", "green", "bold") if body.get("ok") else c("ISSUES FOUND", "red", "bold"))


def cmd_doctor(args) -> int:
    with _client(args) as client:
        resp = client.get("/v1/doctor")
        if not args.pretty:
            return _print_response(resp)
        if resp.status_code >= 400:
            return _print_error_response(resp)
        _print_doctor_pretty(resp.json())
        return 0


def cmd_validate(args) -> int:
    raw = args.json
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON: {exc}"}))
        return 1
    with _client(args) as client:
        resp = client.post("/v1/config/validate", json=data)
        return _print_response(resp)


def cmd_config(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/config"))


def cmd_reload(args) -> int:
    with _client(args) as client:
        return _print_response(client.post("/v1/config/reload"))


def cmd_reload_status(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/config/reload-status"))


def cmd_auth(args) -> int:
    # The one command family that never calls _client()/the REST API at all:
    # it manages the credential that every other command needs to call it in
    # the first place (chicken-and-egg for "rotate", and "show" exists
    # specifically to retrieve the local secret for copying into some other
    # app). Reads/writes the token file directly instead.
    if args.action == "show":
        token = auth.read_token()
        if token is None:
            print(json.dumps({"error": "no auth token configured - run `modelmanager auth rotate`"}))
            return 1
        source = "MODELMANAGER_TOKEN env var" if os.environ.get("MODELMANAGER_TOKEN") else str(auth.default_token_path())
        print(json.dumps({"token": token, "source": source}))
        return 0
    token = auth.generate_token()
    path = auth.write_token(token)
    print(json.dumps({"token": token, "path": str(path), "rotated": True}))
    return 0


def cmd_grant(args) -> int:
    # Local-only, like cmd_auth: reads/writes grants.json directly, no
    # coordinator round-trip. This is User A's own admin action, run on
    # their own machine. `add`/`remove` manage the person's identity (name +
    # token) once; `allow`/`revoke` manage which systems they can reach,
    # one at a time, however many times - see modelmanager/grants.py.
    if args.grant_action == "list":
        entries = grants.load_grants()
        print(json.dumps([
            {"name": g["name"], "allowed_systems": g["allowed_systems"], "created_at": g["created_at"]}
            for g in entries
        ]))
        return 0
    if args.grant_action == "remove":
        removed = grants.remove_grant(args.name)
        print(json.dumps({"name": args.name, "removed": removed}))
        return 0 if removed else 1
    if args.grant_action == "add":
        entry = grants.add_grant(args.name, args.token)
        print(json.dumps({"name": entry["name"], "allowed_systems": entry["allowed_systems"], "created_at": entry["created_at"]}))
        return 0
    # allow/revoke both need a known grant name, and allow additionally
    # validates the system actually exists (revoke doesn't need to - taking
    # away access to a system that was since deleted from the registry is
    # always fine).
    if args.grant_action == "allow":
        try:
            registry = load_registry()
        except (FileNotFoundError, json.JSONDecodeError, ValidationError) as exc:
            print(json.dumps({"error": f"couldn't load the local registry to validate the system: {exc}"}))
            return 1
        if args.system not in registry.systems:
            print(json.dumps({
                "error": f"unknown system {args.system!r} - known systems: {', '.join(sorted(registry.systems))}"
            }))
            return 1
        entry = grants.allow_system(args.name, args.system)
    else:
        entry = grants.revoke_system(args.name, args.system)
    if entry is None:
        print(json.dumps({"error": f"unknown grant {args.name!r} - run `modelmanager grant add` first"}))
        return 1
    print(json.dumps({"name": entry["name"], "allowed_systems": entry["allowed_systems"]}))
    return 0


def cmd_hosts(args) -> int:
    # Local-only, like cmd_auth/cmd_grant: reads/writes known_hosts.json (and
    # the per-host cert PEMs) directly, no coordinator round-trip - this
    # manages which *other* coordinators this machine trusts, which is a
    # decision about this CLI's own TLS trust store, not something any
    # coordinator (even your own) should be able to drive remotely.
    if args.hosts_action == "list":
        entries = known_hosts.load_known_hosts()
        # Most-recently-connected first - lets a caller (the panel's
        # history cycling) just take the array in order, no re-sorting of
        # its own. .get() fallback covers a known_hosts.json written before
        # last_connected existed.
        rows = [
            {
                "name": name,
                "url": e["url"],
                "fingerprint": e["fingerprint"],
                "added_at": e["added_at"],
                "last_connected": e.get("last_connected", e["added_at"]),
            }
            for name, e in entries.items()
        ]
        rows.sort(key=lambda r: r["last_connected"], reverse=True)
        print(json.dumps(rows))
        return 0
    if args.hosts_action == "remove":
        removed = known_hosts.remove_known_host(args.name)
        print(json.dumps({"name": args.name, "removed": removed}))
        return 0 if removed else 1
    if args.hosts_action == "touch":
        entry = known_hosts.touch_known_host(args.name)
        if entry is None:
            print(json.dumps({"error": f"unknown host {args.name!r}"}))
            return 1
        print(json.dumps({"name": args.name, "last_connected": entry["last_connected"]}))
        return 0
    try:
        pem, fingerprint = _fetch_cert_fingerprint(args.url)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": f"couldn't connect to {args.url}: {exc}"}))
        return 1
    if args.hosts_action == "probe":
        print(json.dumps({"url": args.url, "fingerprint": fingerprint}))
        return 0
    # add
    if not args.yes:
        print(f"Fingerprint for {args.url}: {fingerprint}", file=sys.stderr)
        print("Confirm this matches what the other side's operator told you out-of-band.", file=sys.stderr)
        answer = input(f"Trust this certificate and save it as {args.name!r}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print(json.dumps({"error": "not trusted - aborted, nothing saved"}))
            return 1
    entry = known_hosts.add_known_host(args.name, args.url, pem, fingerprint)
    print(json.dumps({"name": args.name, "url": entry["url"], "fingerprint": entry["fingerprint"]}))
    return 0


def cmd_deploy_agent(args) -> int:
    params = {"force": "true"} if args.force else {}
    path = f"/v1/systems/{args.system}/deploy-agent" if args.system else "/v1/deploy-agent"
    with _client(args) as client:
        return _print_response(client.post(path, params=params))


def cmd_put(args) -> int:
    raw = args.json
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON body: {exc}"}))
        return 1
    with _client(args) as client:
        return _print_response(client.post(f"/v1/{args.resource}/{args.name}", json=data))


def _run(ctx: click.Context, fn, **extra) -> None:
    """Every command below ends with this: build the same kind of Namespace
    the cmd_* functions above have always expected (global --url/--token/
    --peer from the root group, plus whatever this command parsed), call it,
    and exit with its return code. This is Click's equivalent of argparse's
    single `sys.exit(args.func(args))` dispatch point - kept as one shared
    helper (rather than inlined per command) so the coordinator-unreachable
    handling below only has to exist once."""
    # dict-merge (not double-star-into-call) so a command's own positional/
    # option wins on a name collision with a global one - `hosts probe
    # <url>` has its own "url" (the target to probe), distinct from the
    # global --url (the coordinator base URL), and argparse's single shared
    # Namespace let the subcommand's own positional win the same way.
    ns = SimpleNamespace(**{**ctx.obj, **extra})
    try:
        sys.exit(fn(ns))
    except httpx.HTTPError as exc:
        # Coordinator unreachable/timed out - one clean JSON line instead of
        # a raw traceback, so callers that just JSON.parse our stdout (e.g.
        # the bar-widget plugin) get a usable error instead of a parse failure.
        target = f"peer {ns.peer!r}" if getattr(ns, "peer", None) else ns.url
        print(json.dumps({"error": f"cannot reach coordinator at {target}: {exc}"}))
        sys.exit(1)


@click.group()
@click.option("--url", default=DEFAULT_URL, help="coordinator base URL")
@click.option(
    "--token", default=None,
    help="bearer token for the coordinator's REST API (defaults to MODELMANAGER_TOKEN env, then the token file - see `modelmanager auth show`)",
)
@click.option(
    "--peer", default=None,
    help="target a remote coordinator by name instead of --url/--token's defaults - "
         "one already trusted via `modelmanager hosts add <name> <url>`",
)
@click.pass_context
def cli(ctx: click.Context, url: str, token: str | None, peer: str | None) -> None:
    ctx.obj = {"url": url, "token": token, "peer": peer}


@cli.command("serve", help="run the coordinator REST service")
@click.option("--host", default=DEFAULT_SERVE_HOST, help="defaults to MODELMANAGER_HOST env, else 0.0.0.0")
@click.option("--port", type=int, default=DEFAULT_SERVE_PORT, help="defaults to MODELMANAGER_PORT env, else 8778")
@click.option("--reload", is_flag=True)
@click.pass_context
def serve(ctx, host, port, reload):
    _run(ctx, cmd_serve, host=host, port=port, reload=reload)


@cli.command("list", help="list model definitions and status")
@click.option("--pretty", "-p", is_flag=True, help="grouped, colored view instead of raw JSON")
@click.pass_context
def list_models(ctx, pretty):
    _run(ctx, cmd_list, pretty=pretty)


@cli.command("summary", help="compact running-model summary")
@click.pass_context
def summary(ctx):
    _run(ctx, cmd_summary)


@cli.command("systems", help="list configured systems")
@click.pass_context
def systems(ctx):
    _run(ctx, cmd_systems)


@cli.command("ensure", help="ensure a model is loaded (no usage lease); a group name ensures every member, best-effort")
@click.argument("name")
@click.option("--dry-run", is_flag=True, help="show what ensure would do (keep/start/swap/blocked) without doing it")
@click.option("--pretty", "-p", is_flag=True, help="human-readable view instead of raw JSON (only affects --dry-run)")
@click.pass_context
def ensure(ctx, name, dry_run, pretty):
    _run(ctx, cmd_ensure, name=name, dry_run=dry_run, pretty=pretty)


@cli.command(
    "verify",
    help="ensure a model is loaded and push a real inference request at it, report ok/latency; "
         "a group name verifies every member, best-effort",
)
@click.argument("name")
@click.option("--timeout", type=float, default=30.0, help="seconds to wait for the test request")
@click.option("--pretty", "-p", is_flag=True, help="human-readable view instead of raw JSON")
@click.pass_context
def verify(ctx, name, timeout, pretty):
    _run(ctx, cmd_verify, name=name, timeout=timeout, pretty=pretty)


@cli.command("acquire", help="load a model and take a usage lease on it, so it can't be evicted/swapped while held")
@click.argument("name")
@click.option("--ttl", type=float, default=None, help="lease lifetime in seconds (default 300)")
@click.pass_context
def acquire(ctx, name, ttl):
    _run(ctx, cmd_acquire, name=name, ttl=ttl)


@cli.command("release", help="release a usage lease taken with `acquire`")
@click.argument("name")
@click.option("--lease-id", default=None, help="omit to release all leases on this model")
@click.pass_context
def release(ctx, name, lease_id):
    _run(ctx, cmd_release, name=name, lease_id=lease_id)


@cli.command("stop", help="stop a running model; a group name stops every member, best-effort")
@click.argument("name")
@click.option("--force", is_flag=True, help="stop even if a usage lease is held")
@click.pass_context
def stop(ctx, name, force):
    _run(ctx, cmd_stop, name=name, force=force)


@cli.command("groups", help="list configured model groups")
@click.pass_context
def groups(ctx):
    _run(ctx, cmd_groups)


@cli.command("group-ensure", help="best-effort: ensure every model in a group is loaded")
@click.argument("name")
@click.pass_context
def group_ensure(ctx, name):
    _run(ctx, cmd_group_ensure, name=name)


@cli.command("group-verify", help="best-effort: verify every model in a group")
@click.argument("name")
@click.option("--timeout", type=float, default=30.0, help="seconds to wait per model's test request")
@click.option("--pretty", "-p", is_flag=True, help="human-readable view instead of raw JSON")
@click.pass_context
def group_verify(ctx, name, timeout, pretty):
    _run(ctx, cmd_group_verify, name=name, timeout=timeout, pretty=pretty)


@cli.command("group-stop", help="best-effort: stop every model in a group")
@click.argument("name")
@click.option("--force", is_flag=True, help="stop even if a usage lease is held")
@click.pass_context
def group_stop(ctx, name, force):
    _run(ctx, cmd_group_stop, name=name, force=force)


@cli.command("group-stats", help="GPU/VRAM stats for every system behind a group's models")
@click.argument("name")
@click.option("--pretty", "-p", is_flag=True, help="human-readable view instead of raw JSON")
@click.pass_context
def group_stats(ctx, name, pretty):
    _run(ctx, cmd_group_stats, name=name, pretty=pretty)


@cli.command("logs", help="show recent logs for a model")
@click.argument("name")
@click.option("--lines", type=int, default=200)
@click.option("--follow", "-f", is_flag=True, help="live-tail the model's actual log file(s)")
@click.pass_context
def logs(ctx, name, lines, follow):
    _run(ctx, cmd_logs, name=name, lines=lines, follow=follow)


@cli.command("log-path", help="where a model's logs actually live (system/control/os/ssh_host/path), for tailing directly")
@click.argument("name")
@click.pass_context
def log_path(ctx, name):
    _run(ctx, cmd_log_path, name=name)


@cli.command("stats", help="show GPU/VRAM stats for a system, a group's systems, or (omitted) every system in one call")
@click.argument("system", required=False, default=None)
@click.option("--pretty", "-p", is_flag=True, help="human-readable view instead of raw JSON")
@click.pass_context
def stats(ctx, system, pretty):
    _run(ctx, cmd_stats, system=system, pretty=pretty)


@cli.command("access-log", help="show recent REST requests (method, path, status, duration)")
@click.option("--lines", type=int, default=200)
@click.pass_context
def access_log(ctx, lines):
    _run(ctx, cmd_access_log, lines=lines)


@cli.command("lifecycle-log", help="show recent model crash/restart events, across every model")
@click.option("--lines", type=int, default=200)
@click.pass_context
def lifecycle_log(ctx, lines):
    _run(ctx, cmd_lifecycle_log, lines=lines)


@cli.command("help", help="show the registry schema and API reference")
@click.pass_context
def help_cmd(ctx):
    _run(ctx, cmd_help)


@cli.command("version", help="show the API/wire-contract version")
@click.pass_context
def version(ctx):
    _run(ctx, cmd_version)


@cli.command("doctor", help="health check: system reachability, plus a dry-run of every model (files present, what ensure would do)")
@click.option("--pretty", "-p", is_flag=True, help="human-readable view instead of raw JSON")
@click.pass_context
def doctor(ctx, pretty):
    _run(ctx, cmd_doctor, pretty=pretty)


@cli.command("validate", help="validate a full registry document without applying it (e.g. before saving over registry.json)")
@click.argument("json_body", metavar="JSON")
@click.pass_context
def validate(ctx, json_body):
    _run(ctx, cmd_validate, json=json_body)


@cli.command("config", help="show the full current registry")
@click.pass_context
def config(ctx):
    _run(ctx, cmd_config)


@cli.command("reload", help="re-read registry.json from disk and apply it live (e.g. after hand-editing a delete)")
@click.pass_context
def reload(ctx):
    _run(ctx, cmd_reload)


@cli.command("reload-status", help="reload_count/last_reload_at, for detecting a reload happened without diffing the registry")
@click.pass_context
def reload_status(ctx):
    _run(ctx, cmd_reload_status)


@cli.command("deploy-agent", help="upload scripts/windows/agent.ps1 to windows/ssh systems if it changed (also runs on coordinator startup)")
@click.argument("system", required=False, default=None)
@click.option("--force", is_flag=True, help="re-upload even if unchanged")
@click.pass_context
def deploy_agent(ctx, system, force):
    _run(ctx, cmd_deploy_agent, system=system, force=force)


@cli.command("put", help="create or update a systems/engines/gpu_groups/models entry")
@click.argument("resource", type=click.Choice(["systems", "engines", "gpu_groups", "models", "groups"]))
@click.argument("name")
@click.argument("json_body", metavar="JSON")
@click.pass_context
def put(ctx, resource, name, json_body):
    _run(ctx, cmd_put, resource=resource, name=name, json=json_body)


@cli.command("auth", help="manage the local REST API bearer token (reads/writes the token file directly - no coordinator needed)")
@click.argument("action", type=click.Choice(["show", "rotate"]))
@click.pass_context
def auth_cmd(ctx, action):
    _run(ctx, cmd_auth, action=action)


@cli.group("grant", help="manage scoped access for other people (reads/writes grants.json directly - no coordinator needed)")
def grant_group():
    pass


@grant_group.command("add", help="register (or re-register) a token someone shared with you under a name - no system access yet")
@click.argument("name")
@click.argument("token")
@click.pass_context
def grant_add(ctx, name, token):
    _run(ctx, cmd_grant, grant_action="add", name=name, token=token)


@grant_group.command("list", help="list grants (never shows tokens)")
@click.pass_context
def grant_list(ctx):
    _run(ctx, cmd_grant, grant_action="list")


@grant_group.command("remove", help="delete a grant entirely by name (identity, token, and all system access)")
@click.argument("name")
@click.pass_context
def grant_remove(ctx, name):
    _run(ctx, cmd_grant, grant_action="remove", name=name)


@grant_group.command("allow", help="give an existing grant access to one more system")
@click.argument("name")
@click.argument("system")
@click.pass_context
def grant_allow(ctx, name, system):
    _run(ctx, cmd_grant, grant_action="allow", name=name, system=system)


@grant_group.command("revoke", help="take away an existing grant's access to one system")
@click.argument("name")
@click.argument("system")
@click.pass_context
def grant_revoke(ctx, name, system):
    _run(ctx, cmd_grant, grant_action="revoke", name=name, system=system)


@cli.group(
    "hosts",
    help="manage trusted remote coordinators by name - trust-on-first-connect "
         "(reads/writes known_hosts.json directly - no coordinator needed)",
)
def hosts_group():
    pass


@hosts_group.command("probe", help="fetch a coordinator's cert fingerprint without trusting it")
@click.argument("url")
@click.pass_context
def hosts_probe(ctx, url):
    _run(ctx, cmd_hosts, hosts_action="probe", url=url)


@hosts_group.command("add", help="trust a coordinator's cert and save it under a name")
@click.argument("name")
@click.argument("url")
@click.option("--yes", is_flag=True, help="skip the interactive fingerprint confirmation")
@click.pass_context
def hosts_add(ctx, name, url, yes):
    _run(ctx, cmd_hosts, hosts_action="add", name=name, url=url, yes=yes)


@hosts_group.command("list", help="list trusted hosts")
@click.pass_context
def hosts_list(ctx):
    _run(ctx, cmd_hosts, hosts_action="list")


@hosts_group.command("remove", help="stop trusting a host by name")
@click.argument("name")
@click.pass_context
def hosts_remove(ctx, name):
    _run(ctx, cmd_hosts, hosts_action="remove", name=name)


@hosts_group.command("touch", help="bump a host's last-connected time (used internally when actually switching to it)")
@click.argument("name")
@click.pass_context
def hosts_touch(ctx, name):
    _run(ctx, cmd_hosts, hosts_action="touch", name=name)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
