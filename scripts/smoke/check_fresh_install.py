#!/usr/bin/env python3
"""Fails if `modelmanager` isn't running this exact working copy's code.

scripts/setup.sh installs a plain (non-
editable) copy into ~/.local/share/modelmanager/venv - tiers 2-4 exercise
whatever's already installed and running there, so a code change here
needs a redeploy (setup.sh, or the `pip install --force-reinstall` +
`systemctl --user restart modelmanager` this session used directly) before
those tiers actually test it. Without this check, `all.sh` could report a
clean pass while silently testing the previous version of the code.

Usage: check_fresh_install.py [--skip-process-check] [path-to-modelmanager-binary]
Defaults to whatever `modelmanager` is on PATH - pass an explicit path for a
caller whose actual binary isn't that one (e.g. the Omarchy bar widget,
which resolves its own MODELMANAGER_BIN/fixed venv path independently of
this shell's PATH - see Panel.qml's mmBin).

--skip-process-check only checks the files on disk, not whether a local
modelmanager.service has loaded them - for a caller whose actual target is
a remote peer (the local CLI binary's freshness still matters there, since
it's what builds the --peer request, but there's no reason a local
coordinator even has to be running).
"""
from __future__ import annotations

import filecmp
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse


def _diff_dirs(a: Path, b: Path) -> list[str]:
    diffs: list[str] = []
    # _vendor is a real, always-present build artifact (pyproject.toml's
    # [tool.hatch.build.targets.wheel.force-include] packages
    # scripts/windows/agent.ps1 into modelmanager/_vendor/agent.ps1 at
    # build time) - it legitimately exists only in the installed copy,
    # never in this raw source tree, so flagging it as a "diff" was a
    # false positive unrelated to whether the actual Python code is stale.
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__", "_vendor"])
    diffs += [str(a / f) for f in cmp.left_only]
    diffs += [str(b / f) for f in cmp.right_only]
    diffs += [str(a / f) for f in cmp.funny_files]
    # Not cmp.diff_files/same_files - those come from filecmp.cmp's default
    # shallow=True, which calls two files equal whenever os.stat() agrees
    # (size + mtime) without ever reading their content. A reinstall that
    # happens to preserve mtimes (or just bad luck) could let genuinely
    # different code pass this gate, so force a real byte comparison.
    for f in cmp.common_files:
        if not filecmp.cmp(a / f, b / f, shallow=False):
            diffs.append(str(a / f))
    for name, sub in cmp.subdirs.items():
        diffs += _diff_dirs(a / name, b / name)
    return diffs


def _decode_proc_address(hex_addr: str) -> str:
    """Decodes /proc/net/tcp[6]'s hex-encoded local/remote IP into a normal
    address string. IPv4 fields are 4 bytes in host byte order (reversed
    from network order); IPv6 fields are 4 words of 4 bytes, each word
    individually byte-reversed the same way."""
    raw = bytes.fromhex(hex_addr)
    if len(raw) == 4:
        packed = raw[::-1]
        family = socket.AF_INET
    else:
        packed = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
        family = socket.AF_INET6
    return socket.inet_ntop(family, packed)


def _listening_sockets(pid: str) -> set[tuple[str, int]]:
    """(bind-address, port) pairs this specific process has LISTENing
    sockets on.

    /proc/{pid}/net/tcp* isn't actually per-process - confirmed directly:
    without network-namespace isolation (the normal case outside
    containers), it's identical to /proc/net/tcp* for every pid on the
    box, so comparing against it would really be asking "is anything on
    the whole system listening on this port", not "is this process". The
    real per-process view is /proc/{pid}/fd/*'s socket inodes intersected
    with /proc/net/tcp*'s (global) inode column.

    Returns (address, port) pairs, not port alone - a fresh coordinator
    on 127.0.0.1:8778 and a stale one on [::1]:8778 would otherwise be
    indistinguishable, letting the stale one pass under cover of the
    fresh one's port number."""
    try:
        fd_dir = Path(f"/proc/{pid}/fd")
        owned_inodes = set()
        for entry in fd_dir.iterdir():
            try:
                target = entry.resolve()
            except OSError:
                continue
            name = target.name
            if name.startswith("socket:[") and name.endswith("]"):
                owned_inodes.add(name[len("socket:["):-1])
    except OSError:
        return set()

    sockets: set[tuple[str, int]] = set()
    for proto_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proto_file) as f:
                lines = f.readlines()[1:]  # skip header
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            local_address, state, inode = fields[1], fields[3], fields[9]
            if state != "0A" or inode not in owned_inodes:  # TCP_LISTEN
                continue
            try:
                hex_addr, hex_port = local_address.rsplit(":", 1)
                sockets.add((_decode_proc_address(hex_addr), int(hex_port, 16)))
            except (IndexError, ValueError):
                continue
    return sockets


def _effective_local_url() -> str:
    """Mirrors modelmanager/cli.py's own DEFAULT_URL resolution - same
    MODELMANAGER_URL/MODELMANAGER_PORT overrides, same 127.0.0.1:8778
    fallback - so this names the same address a bare `modelmanager`
    invocation (no --url/--peer) would actually hit."""
    url = os.environ.get("MODELMANAGER_URL")
    if url:
        return url
    port = os.environ.get("MODELMANAGER_PORT", "8778")
    return f"https://127.0.0.1:{port}"


def _coordinator_process_issue(installed_pkg: Path, interpreter: str) -> str | None:
    """None if the *running* modelmanager.service process is this exact
    installation and started after its newest file change, else a
    description of why not.

    Matching files on disk isn't
    enough - cmd_serve's uvicorn process imports modelmanager once at
    startup (no --reload in production), so `pip install --force-reinstall`
    without restarting the service leaves fresh files on disk while the
    already-running process keeps serving the old code from memory. That's
    exactly the class of false-OK this whole script exists to prevent."""
    pid = None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", "modelmanager.service"],
            capture_output=True, text=True, timeout=5,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value.isdigit() and value != "0":
            pid = value
    except OSError:
        pass
    if pid is None:
        try:
            result = subprocess.run(["pgrep", "-nf", "modelmanager serve"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().splitlines()[0]
        except OSError:
            pass
    if pid is None:
        return ("couldn't find a running coordinator process (systemctl/pgrep) - can't confirm it's actually "
                "serving this fresh code, only that the files on disk are fresh")

    # A running process that
    # merely started after installed_pkg's newest mtime isn't proof it's
    # running *that* installation - modelmanager.service could be a
    # completely different venv that happens to have started recently for
    # an unrelated reason. The first fix for this compared /proc/{pid}/exe
    # (resolved) against the venv directory - wrong, because a venv's
    # python is commonly a symlink to a shared base interpreter (confirmed
    # directly: this box's venv/bin/python3 resolves to a mise-managed
    # interpreter many unrelated venvs share, making that comparison
    # meaningless). /proc/{pid}/cmdline's argv[0] is the kernel's unresolved
    # shebang-interpreter path (confirmed directly too) - exactly `interpreter`
    # below, parsed the same way from this same mm_bin's own shebang line -
    # so comparing those two literal strings actually identifies the venv.
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv0 = f.read().split(b"\0")[0].decode()
    except (OSError, IndexError, UnicodeDecodeError):
        return f"couldn't read /proc/{pid}/cmdline to confirm which installation it's actually running"
    if argv0 != interpreter:
        return (f"the running coordinator (pid {pid}) was launched as {argv0!r}, not {interpreter!r} - "
                f"it's a different installation; this check can't tell you anything about whether *that* "
                f"one is fresh")

    # None of the above matters if a bare
    # `modelmanager` call (no --url/--peer, which is exactly what a local
    # caller makes) wouldn't even reach this process - MODELMANAGER_URL
    # overrides cli.py's own default the same way it would for the caller,
    # so if it's set to something non-loopback, this coordinator being
    # fresh says nothing about what the caller's requests actually hit.
    effective_url = _effective_local_url()
    parsed = urlparse(effective_url)
    effective_host = parsed.hostname
    if effective_host not in ("127.0.0.1", "localhost", "::1"):
        return (f"MODELMANAGER_URL/MODELMANAGER_PORT resolves to {effective_url!r}, not a loopback address - "
                f"a bare `modelmanager` call wouldn't even reach this coordinator, so its freshness doesn't "
                f"say anything about what the caller's requests actually hit")

    # Loopback alone isn't enough -
    # a *different*, stale coordinator could be listening on some other
    # port (e.g. MODELMANAGER_PORT=9778 while this fresh one is on 8778,
    # both loopback). Confirm the effective port is one this exact process
    # is actually listening on, not just that the host matches.
    #
    # A bare scheme-only URL (e.g.
    # "https://localhost", no explicit port) has parsed.port=None - the
    # real default is the scheme's own standard port, not a hardcoded
    # 8778 (which only happens to be right because that's this project's
    # own convention, not because of anything the URL itself says).
    default_port = {"https": 443, "http": 80}.get(parsed.scheme, 8778)
    effective_port = parsed.port or default_port
    # Matching port alone, ignoring the bind
    # address, would let a stale coordinator on a different address family pass
    # under cover of a fresh one's port number (127.0.0.1:8778 vs
    # [::1]:8778 are different sockets that happen to share a port).
    # "localhost" is ambiguous (could resolve to either family depending
    # on /etc/hosts/NSS), so accept either; a wildcard bind (0.0.0.0/::)
    # legitimately serves both.
    acceptable_addrs = {
        "127.0.0.1": {"127.0.0.1", "0.0.0.0"},
        "::1": {"::1", "::"},
        "localhost": {"127.0.0.1", "::1", "0.0.0.0", "::"},
    }.get(effective_host, {effective_host})
    sockets = _listening_sockets(pid)
    if not any(addr in acceptable_addrs and port == effective_port for addr, port in sockets):
        return (f"MODELMANAGER_URL/MODELMANAGER_PORT selects {effective_host}:{effective_port}, but pid "
                f"{pid} isn't listening there (listening on: {sorted(sockets) or 'nothing'}) - a bare "
                f"`modelmanager` call would reach a different coordinator than the one just checked for "
                f"freshness")

    try:
        etimes = subprocess.run(["ps", "-o", "etimes=", "-p", pid], capture_output=True, text=True, timeout=5)
        elapsed_seconds = int(etimes.stdout.strip())
    except (OSError, ValueError):
        return f"couldn't determine how long coordinator process {pid} has been running"
    proc_start_epoch = time.time() - elapsed_seconds

    newest_mtime = max((f.stat().st_mtime for f in installed_pkg.rglob("*.py")), default=0)
    if proc_start_epoch < newest_mtime:
        return (f"the running coordinator (pid {pid}) started before the installed files' newest change - "
                f"it's still serving the old code from memory regardless of what's on disk now; restart it: "
                f"systemctl --user restart modelmanager")
    return None


def main() -> int:
    repo_dir = Path(__file__).resolve().parents[2]
    repo_pkg = repo_dir / "modelmanager"

    argv = sys.argv[1:]
    skip_process_check = "--skip-process-check" in argv
    positional = [a for a in argv if a != "--skip-process-check"]

    mm_bin = positional[0] if positional else shutil.which("modelmanager")
    if not mm_bin:
        print("error: no `modelmanager` on PATH - can't check what tiers 2-4 would actually test", file=sys.stderr)
        return 1

    with open(mm_bin) as f:
        shebang = f.readline().strip()
    if not shebang.startswith("#!"):
        print(f"error: {mm_bin} has no shebang - can't determine its interpreter", file=sys.stderr)
        return 1
    interpreter = shebang[2:].split()[0]

    # -I (isolated mode) is load-bearing, not cosmetic: without it, `-c`'s
    # implicit sys.path[0] of "" (the process's cwd) shadows the actual
    # installed site-packages copy whenever this runs from the repo root
    # (which it always does) - confirmed directly: the exact same command
    # without -I silently resolved `import modelmanager` back to this
    # working copy's own source, regardless of what's really installed,
    # making this check always pass. cwd=tempfile.gettempdir() is belt-and-
    # suspenders against the same shadowing via any other path.
    result = subprocess.run(
        [interpreter, "-I", "-c", "import modelmanager, os; print(os.path.dirname(modelmanager.__file__))"],
        capture_output=True, text=True, cwd=tempfile.gettempdir(),
    )
    if result.returncode != 0:
        print(f"error: couldn't locate the installed modelmanager package: {result.stderr.strip()}", file=sys.stderr)
        return 1
    installed_pkg = Path(result.stdout.strip())

    diffs = _diff_dirs(repo_pkg, installed_pkg)
    # _diff_dirs ignores _vendor
    # wholesale (it's not sourced from modelmanager/ at all - see the
    # comment there), which meant a change to scripts/windows/agent.ps1
    # itself, not yet rebuilt into modelmanager/_vendor/agent.ps1, passed
    # silently. Compare that one file against its real source directly.
    agent_src = repo_dir / "scripts" / "windows" / "agent.ps1"
    agent_vendored = installed_pkg / "_vendor" / "agent.ps1"
    if agent_src.exists() != agent_vendored.exists() or (
        agent_src.exists() and not filecmp.cmp(agent_src, agent_vendored, shallow=False)
    ):
        diffs.append(f"{agent_src} (packaged copy at {agent_vendored} is out of date)")
    if diffs:
        print(f"error: {mm_bin}'s installed modelmanager ({installed_pkg}) doesn't match this working copy "
              f"({repo_pkg}) - tiers 2-4 would be testing stale code. Redeploy first, e.g.:", file=sys.stderr)
        print(f"  {interpreter.rsplit('/bin/', 1)[0]}/bin/pip install --no-deps --force-reinstall {repo_dir} "
              f"&& systemctl --user restart modelmanager", file=sys.stderr)
        print("Differing files:", file=sys.stderr)
        for d in diffs[:20]:
            print(f"  {d}", file=sys.stderr)
        return 1

    if skip_process_check:
        print(f"OK: {mm_bin}'s installed modelmanager ({installed_pkg}) matches this working copy "
              f"(process check skipped)")
        return 0

    process_issue = _coordinator_process_issue(installed_pkg, interpreter)
    if process_issue:
        print(f"error: {process_issue}", file=sys.stderr)
        return 1

    print(f"OK: {mm_bin}'s installed modelmanager ({installed_pkg}) matches this working copy, "
          f"and the running coordinator has loaded it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
