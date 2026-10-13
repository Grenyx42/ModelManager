from __future__ import annotations

from .errors import CommandFailedError
from .runners.base import Runner

QUERY_FIELDS = ["index", "name", "memory.used", "memory.total", "utilization.gpu", "temperature.gpu"]
NVIDIA_SMI_CMD = (
    "nvidia-smi --query-gpu=" + ",".join(QUERY_FIELDS) + " --format=csv,noheader,nounits"
)


def gpu_stats(runner: Runner, timeout: float = 15) -> list[dict]:
    result = runner.run_shell(NVIDIA_SMI_CMD, timeout=timeout)
    if not result.ok:
        raise CommandFailedError(f"nvidia-smi failed: {result.stderr.strip() or result.stdout.strip()}")
    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(QUERY_FIELDS):
            continue
        index, name, mem_used, mem_total, util, temp = parts
        gpus.append(
            {
                "index": int(index),
                "name": name,
                "memory_used_mb": int(mem_used),
                "memory_total_mb": int(mem_total),
                "utilization_pct": int(util),
                "temperature_c": int(temp),
            }
        )
    return gpus
