import pytest

from modelmanager import cpu_stats
from modelmanager.errors import CommandFailedError
from modelmanager.runners.base import CommandResult, Runner


class FakeRunner(Runner):
    def __init__(self, result: CommandResult):
        self.result = result

    def run(self, cmd, timeout=30):
        raise NotImplementedError

    def run_shell(self, cmd, timeout=30):
        return self.result


def test_parse_proc_stat_line_sums_busy_and_idle_fields():
    # user nice system idle iowait irq softirq steal guest guest_nice -
    # guest/guest_nice are ignored (already counted in user/nice upstream).
    busy, idle = cpu_stats._parse_proc_stat_line("cpu  10 2 3 100 4 0 1 0 0 0")

    assert (busy, idle) == (16, 104)


def test_parse_meminfo_keys_by_name_not_position():
    busy = cpu_stats._parse_meminfo(["MemAvailable:    2048000 kB", "MemTotal:        8192000 kB"])

    assert busy == (8192000, 2048000)


def test_cpu_stats_linux_parses_two_samples_and_meminfo():
    stdout = "\n".join(
        [
            "cpu  0 0 0 0 0 0 0 0 0 0",
            "cpu  20 0 0 80 0 0 0 0 0 0",
            "MemTotal:        8192000 kB",
            "MemAvailable:    2048000 kB",
        ]
    )
    runner = FakeRunner(CommandResult(0, stdout, ""))

    result = cpu_stats.cpu_stats(runner, "linux")

    assert result == {"utilization_pct": 20, "memory_used_mb": 6000, "memory_total_mb": 8000}


def test_cpu_stats_linux_identical_samples_report_zero_not_divide_by_zero():
    stdout = "\n".join(
        [
            "cpu  100 0 0 900 0 0 0 0 0 0",
            "cpu  100 0 0 900 0 0 0 0 0 0",
            "MemTotal:        1024000 kB",
            "MemAvailable:    512000 kB",
        ]
    )
    runner = FakeRunner(CommandResult(0, stdout, ""))

    result = cpu_stats.cpu_stats(runner, "linux")

    assert result["utilization_pct"] == 0


def test_cpu_stats_linux_command_failure_raises():
    runner = FakeRunner(CommandResult(1, "", "no such file or directory"))

    with pytest.raises(CommandFailedError):
        cpu_stats.cpu_stats(runner, "linux")


def test_cpu_stats_windows_parses_newline_separated_output():
    # PowerShell auto-outputs bare trailing expressions ($cpu; $used; $total)
    # one per line - confirmed directly against a real box that a quoted,
    # comma-joined Write-Output string loses its quotes in the SSH -> remote
    # -shell transport, so this is the format that's actually reliable.
    runner = FakeRunner(CommandResult(0, "23\n3456\n8192\n", ""))

    result = cpu_stats.cpu_stats(runner, "windows")

    assert result == {"utilization_pct": 23, "memory_used_mb": 3456, "memory_total_mb": 8192}


def test_cpu_stats_windows_command_failure_raises():
    runner = FakeRunner(CommandResult(1, "", "powershell: command not found"))

    with pytest.raises(CommandFailedError):
        cpu_stats.cpu_stats(runner, "windows")
