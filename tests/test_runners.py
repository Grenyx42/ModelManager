import time

from modelmanager.runners.local import LocalRunner


def test_run_captures_output():
    runner = LocalRunner()
    result = runner.run(["echo", "hello"])
    assert result.ok
    assert result.stdout.strip() == "hello"


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


def test_tail_returns_last_lines(tmp_path):
    runner = LocalRunner()
    log_path = tmp_path / "out.log"
    log_path.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")

    tail = runner.tail(log_path, lines=3)

    assert tail.strip().splitlines() == ["line7", "line8", "line9"]


def test_tail_missing_file_returns_empty(tmp_path):
    runner = LocalRunner()
    assert runner.tail(tmp_path / "missing.log", lines=10) == ""
