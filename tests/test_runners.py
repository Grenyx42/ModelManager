import os
import shlex
import subprocess
import time

import pytest

from modelmanager.errors import CommandTimeoutError, HostUnreachableError
from modelmanager.runners.base import CommandResult
from modelmanager.runners.local import LocalRunner
from modelmanager.runners.ssh import SSHRunner


def test_run_captures_output():
    runner = LocalRunner()
    result = runner.run(["echo", "hello"])
    assert result.ok
    assert result.stdout.strip() == "hello"


def test_run_tolerates_non_utf8_output():
    # Confirmed directly in production: a model's own log output containing
    # a non-UTF-8 byte crashed subprocess's strict decode, turning a routine
    # logs request into a raw 500 instead of just garbling that one byte.
    #
    # Asserting only "ok"/"done" are present
    # would also pass if the invalid byte were silently dropped instead of
    # replaced, and wouldn't catch encoding="utf-8" being missing (a non-
    # UTF-8 locale could mis-decode the *valid* "café" bytes too) - assert
    # the whole string, including the replacement character.
    runner = LocalRunner()
    result = runner.run(["printf", "ok \\xe6 caf\\xc3\\xa9 done"])
    assert result.ok
    assert result.stdout == "ok � café done"


def test_run_shell_timeout_raises_command_timeout_error():
    runner = LocalRunner()
    with pytest.raises(CommandTimeoutError):
        runner.run_shell("sleep 5", timeout=0.2)


def test_run_shell_tolerates_non_utf8_output():
    # run() has a non-UTF-8 test, but
    # run_shell() got the same encoding="utf-8"/errors="replace" fix
    # without its own test - nothing was exercising that decode path here.
    #
    # run_shell uses shell=True, i.e. /bin/sh -
    # dash's printf builtin (POSIX, no GNU extensions) doesn't understand
    # \xHH hex escapes and would print them literally instead of emitting
    # the intended bytes. Octal escapes (\NNN) are POSIX and portable to
    # both dash and bash.
    runner = LocalRunner()
    result = runner.run_shell("printf 'ok \\346 caf\\303\\251 done'")
    assert result.ok
    assert result.stdout == "ok � café done"


def test_tail_tolerates_non_utf8_output(tmp_path):
    # Same gap, for tail() - it decodes `tail`'s own subprocess output with
    # the identical fix, but had no test covering invalid bytes either.
    log_path = tmp_path / "out.log"
    log_path.write_bytes(b"ok \xe6 caf\xc3\xa9 done\n")
    runner = LocalRunner()
    assert runner.tail(log_path, lines=10) == "ok � café done\n"


def test_ssh_run_tolerates_non_utf8_output():
    # The other non-UTF-8 test (test_run_tolerates_non_utf8_output) only
    # exercises LocalRunner - the SSH-specific tests all mock subprocess.run
    # entirely, so none of them actually run SSHRunner._run's own decoding.
    # Calling _run directly with a real local command (bypassing the ssh/scp
    # wrapping, which _run doesn't care about - it just execs whatever list
    # it's given) exercises the real decode path without a live SSH host.
    runner = SSHRunner("monolith")
    result = runner._run(["printf", "ok \\xe6 caf\\xc3\\xa9 done"], timeout=5)
    assert result.stdout == "ok � café done"


def test_ssh_run_quotes_argv_for_a_posix_remote_shell_not_windows(monkeypatch):
    # run()'s list form has to become a single string SSH hands to the
    # remote shell - that shell is never cmd.exe, so the joining has to use
    # POSIX quoting (shlex.join), not subprocess.list2cmdline's Windows
    # escaping, which doesn't even quote $, `, or ' the way a POSIX shell
    # needs - an argument containing one of those would be silently
    # mis-executed on the remote end instead of passed through literally.
    captured = {}

    def fake_run_shell(self, cmd, timeout=30):
        captured["cmd"] = cmd
        return CommandResult(0, "", "")

    monkeypatch.setattr(SSHRunner, "run_shell", fake_run_shell)
    runner = SSHRunner("monolith")

    runner.run(["echo", "$HOME has a ' quote and a space"])

    assert captured["cmd"] == "echo '$HOME has a '\"'\"' quote and a space'"
    assert shlex.split(captured["cmd"]) == ["echo", "$HOME has a ' quote and a space"]


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


def test_concurrent_stop_on_the_same_pid_does_not_raise(tmp_path):
    # Found by the tier-5 adversarial smoke test: two force-stop requests
    # for the same model landing concurrently (ordinary under real
    # concurrent use, not a crafted attack) both pass the `_children.get`
    # check, both successfully proc.wait(), then raced on `del
    # self._children[pid]` - the second one raised KeyError, surfacing as
    # a raw HTTP 500 to whoever's request lost the race.
    import threading

    runner = LocalRunner()
    log_path = tmp_path / "proc.log"
    pid_path = tmp_path / "proc.pid"
    log_path.touch()
    pid = runner.start_background(["sleep", "5"], log_path, pid_path)

    errors = []

    def stop_it():
        try:
            runner.stop(pid, timeout=2)
        except Exception as exc:  # noqa: BLE001 - the whole point is "did this raise at all"
            errors.append(exc)

    threads = [threading.Thread(target=stop_it) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []
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
