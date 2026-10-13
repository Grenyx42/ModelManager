import os
import subprocess
import time

import pytest

from modelmanager.errors import CommandTimeoutError, HostUnreachableError
from modelmanager.runners.local import LocalRunner
from modelmanager.runners.ssh import SSHRunner


def test_run_captures_output():
    runner = LocalRunner()
    result = runner.run(["echo", "hello"])
    assert result.ok
    assert result.stdout.strip() == "hello"


def test_run_shell_timeout_raises_command_timeout_error():
    runner = LocalRunner()
    with pytest.raises(CommandTimeoutError):
        runner.run_shell("sleep 5", timeout=0.2)


def test_ssh_run_shell_timeout_raises_command_timeout_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = SSHRunner("monolith")
    with pytest.raises(CommandTimeoutError):
        runner.run_shell("echo hi", timeout=1)


def test_ssh_run_shell_connection_failure_raises_host_unreachable_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 255, stdout="", stderr="ssh: connect to host monolith port 22: Connection refused"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = SSHRunner("monolith")
    with pytest.raises(HostUnreachableError, match="Connection refused"):
        runner.run_shell("echo hi", timeout=5)


def test_ssh_run_shell_remote_command_failure_is_not_host_unreachable(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="some remote error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = SSHRunner("monolith")
    result = runner.run_shell("echo hi", timeout=5)
    assert result.returncode == 1
    assert not result.ok


def test_start_background_tracks_pid_and_can_be_stopped(tmp_path):
    runner = LocalRunner()
    log_path = tmp_path / "proc.log"
    pid_path = tmp_path / "proc.pid"
    log_path.touch()

    pid = runner.start_background(["sleep", "5"], log_path, pid_path)

    assert pid_path.read_text().strip() == str(pid)
    assert runner.is_running(pid)

    runner.stop(pid, timeout=2)
    time.sleep(0.1)
    assert not runner.is_running(pid)


def test_start_background_merges_extra_env(tmp_path):
    runner = LocalRunner()
    log_path = tmp_path / "proc.log"
    pid_path = tmp_path / "proc.pid"
    log_path.touch()

    pid = runner.start_background(
        ["sh", "-c", "echo $MY_CUSTOM_VAR; echo $HOME"],
        log_path,
        pid_path,
        extra_env={"MY_CUSTOM_VAR": "hello-cuda"},
    )
    for _ in range(20):
        if not runner.is_running(pid):
            break
        time.sleep(0.1)

    lines = log_path.read_text().strip().splitlines()
    assert "launching:" in lines[0]  # the header line start_background writes first
    assert lines[1] == "hello-cuda"
    assert lines[2] == os.environ.get("HOME", "")  # base environment wasn't dropped, just extended


def test_start_background_writes_cmdline_header(tmp_path):
    runner = LocalRunner()
    log_path = tmp_path / "proc.log"
    pid_path = tmp_path / "proc.pid"
    log_path.touch()

    pid = runner.start_background(["sh", "-c", "exit 0"], log_path, pid_path, cwd=str(tmp_path))
    for _ in range(20):
        if not runner.is_running(pid):
            break
        time.sleep(0.1)

    header = log_path.read_text().splitlines()[0]
    assert "launching: sh -c 'exit 0'" in header
    assert f"cwd={tmp_path}" in header


def test_tail_returns_last_lines(tmp_path):
    runner = LocalRunner()
    log_path = tmp_path / "out.log"
    log_path.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")

    tail = runner.tail(log_path, lines=3)

    assert tail.strip().splitlines() == ["line7", "line8", "line9"]


def test_tail_missing_file_returns_empty(tmp_path):
    runner = LocalRunner()
    assert runner.tail(tmp_path / "missing.log", lines=10) == ""
