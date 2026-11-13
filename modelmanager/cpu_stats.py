from __future__ import annotations

from .errors import CommandFailedError
from .runners.base import Runner

# Two /proc/stat samples 0.3s apart, chained into one command so this only
# costs one SSH round trip - a second `run_shell` call for the next sample
# would double the latency on a remote system for no benefit.
LINUX_CMD = (
    "head -n1 /proc/stat && sleep 0.3 && head -n1 /proc/stat && "
    "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo"
)

# Single-quoted (not double) because the remote shell an SSH exec lands in
# can itself be PowerShell, which would interpolate $cpu/$os/$used/$total in
# this string before the inner, explicitly-invoked powershell.exe ever sees
# it - same reasoning as remote_paths.py's $env:USERPROFILE resolution.
# CIM's LoadPercentage is already an interval-sampled average (unlike
# /proc/stat's raw counters), so one call is enough - no sleep needed here.
#
# Bare trailing expressions ($cpu; $used; $total), not a quoted/interpolated
# Write-Output string - confirmed directly against a real box that an
# embedded "$cpu,$used,$total" loses its double quotes somewhere in the SSH
# -> remote-shell transport, so PowerShell parsed it as the comma *operator*
# building an array instead of one joined string, and Write-Output of an
# array prints one element per line. Bare expressions need no quoting at
# all, so there's nothing left to strip - PowerShell auto-outputs each one,
# already one per line, which is exactly what _windows_stats() below parses.
WINDOWS_CMD = (
    "powershell -NoProfile -Command "
    "'$cpu=[math]::Round((Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average); "
    "$os=Get-CimInstance Win32_OperatingSystem; "
    "$used=[math]::Round(($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/1024); "
    "$total=[math]::Round($os.TotalVisibleMemorySize/1024); "
    "$cpu; $used; $total'"
)


def _parse_proc_stat_line(line: str) -> tuple[int, int]:
    """Returns (busy, idle) jiffies from a `cpu  ...` /proc/stat line."""
    fields = [int(x) for x in line.split()[1:]]
    fields += [0] * (8 - len(fields))  # older kernels omit irq/softirq/steal
    user, nice, system, idle, iowait, irq, softirq, steal = fields[:8]
    return user + nice + system + irq + softirq + steal, idle + iowait


def _parse_meminfo(lines: list[str]) -> tuple[int, int]:
    """Returns (MemTotal, MemAvailable) in kB, keyed by name rather than
    position - /proc/meminfo's field order is stable in practice but not
    a documented contract."""
    values = {}
    for line in lines:
        key, _, rest = line.partition(":")
        values[key.strip()] = int(rest.strip().split()[0])
    return values["MemTotal"], values["MemAvailable"]


def _linux_stats(runner: Runner, timeout: float) -> dict:
    result = runner.run_shell(LINUX_CMD, timeout=timeout)
    if not result.ok:
        raise CommandFailedError(f"cpu stats failed: {result.stderr.strip() or result.stdout.strip()}")
    lines = result.stdout.strip().splitlines()
    if len(lines) < 4:
        raise CommandFailedError(f"cpu stats: unexpected output {result.stdout!r}")
    busy1, idle1 = _parse_proc_stat_line(lines[0])
    busy2, idle2 = _parse_proc_stat_line(lines[1])
    total_kb, avail_kb = _parse_meminfo(lines[2:])
    busy_delta = busy2 - busy1
    total_delta = busy_delta + (idle2 - idle1)
    # Two samples 0.3s apart can land on the same tick on an otherwise-idle
    # box - report 0% rather than divide by zero.
    pct = round(100 * busy_delta / total_delta) if total_delta > 0 else 0
    return {
        "utilization_pct": pct,
        "memory_used_mb": (total_kb - avail_kb) // 1024,
        "memory_total_mb": total_kb // 1024,
    }


def _windows_stats(runner: Runner, timeout: float) -> dict:
    result = runner.run_shell(WINDOWS_CMD, timeout=timeout)
    if not result.ok:
        raise CommandFailedError(f"cpu stats failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        pct, used, total = result.stdout.split()
        return {
            "utilization_pct": int(round(float(pct))),
            "memory_used_mb": int(round(float(used))),
            "memory_total_mb": int(round(float(total))),
        }
    except ValueError as exc:
        raise CommandFailedError(f"cpu stats: unexpected output {result.stdout!r}") from exc


def cpu_stats(runner: Runner, os_type: str, timeout: float = 15) -> dict:
    if os_type == "windows":
        return _windows_stats(runner, timeout)
    return _linux_stats(runner, timeout)
