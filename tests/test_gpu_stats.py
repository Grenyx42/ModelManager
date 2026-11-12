import pytest

from modelmanager import gpu_stats
from modelmanager.errors import CommandFailedError
from modelmanager.runners.base import CommandResult, Runner


class FakeRunner(Runner):
    def __init__(self, result: CommandResult):
        self.result = result

    def run(self, cmd, timeout=30):
        raise NotImplementedError

    def run_shell(self, cmd, timeout=30):
        return self.result


def test_gpu_stats_parses_csv_output():
    runner = FakeRunner(CommandResult(0, "0, NVIDIA GeForce RTX 4090, 1024, 24576, 12, 55\n", ""))

    result = gpu_stats.gpu_stats(runner)

    assert result == [
        {
            "index": 0,
            "name": "NVIDIA GeForce RTX 4090",
            "memory_used_mb": 1024,
            "memory_total_mb": 24576,
            "utilization_pct": 12,
            "temperature_c": 55,
        }
    ]


def test_gpu_stats_missing_binary_is_no_gpus_not_an_error():
    # Exit 127 is the shell's own "command not found" - a CPU-only box (no
    # NVIDIA card, nothing to install) shouldn't surface as a failure.
    runner = FakeRunner(CommandResult(127, "", "bash: nvidia-smi: command not found"))

    assert gpu_stats.gpu_stats(runner) == []


def test_gpu_stats_real_failure_still_raises():
    runner = FakeRunner(CommandResult(1, "", "Failed to initialize NVML: Driver/library version mismatch"))

    with pytest.raises(CommandFailedError):
        gpu_stats.gpu_stats(runner)
