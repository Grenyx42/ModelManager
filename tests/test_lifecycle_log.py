from modelmanager import lifecycle_log, paths


def test_tail_returns_empty_when_no_log_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert lifecycle_log.tail() == ""


def test_tail_tolerates_non_utf8_output(tmp_path, monkeypatch):
    # Found by Claude Code's review: this tail() decodes `tail`'s own
    # subprocess output the same way runners/local.py's tail() does, but
    # had no test covering invalid bytes - a non-UTF-8 byte here would
    # otherwise crash GET /v1/lifecycle-log.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log_path = paths.lifecycle_log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"ok \xe6 caf\xc3\xa9 done\n")
    assert lifecycle_log.tail() == "ok � café done\n"
