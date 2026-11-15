import logging

from modelmanager import access_log, paths


def test_tail_returns_empty_when_no_log_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert access_log.tail() == ""


def test_configure_then_record_writes_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    access_log.configure()
    access_log.record("127.0.0.1:12345", "GET", "/v1/models", 200, 3.4)

    text = access_log.tail()
    assert "127.0.0.1:12345 GET /v1/models -> 200 (3.4ms)" in text


def test_configure_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    access_log.configure()
    access_log.configure()
    logger = logging.getLogger(access_log.LOGGER_NAME)

    marked = [h for h in logger.handlers if getattr(h, "_modelmanager_access", False)]
    assert len(marked) == 1


def test_tail_tolerates_non_utf8_output(tmp_path, monkeypatch):
    # Found by Claude Code's review: this tail() decodes `tail`'s own
    # subprocess output the same way runners/local.py's tail() does, but
    # had no test covering invalid bytes - a non-UTF-8 byte here (e.g. a
    # client/path logged verbatim) would otherwise crash GET /v1/access-log.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log_path = paths.access_log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"ok \xe6 caf\xc3\xa9 done\n")
    assert access_log.tail() == "ok � café done\n"


def test_tail_respects_line_count(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    access_log.configure()

    for i in range(5):
        access_log.record("127.0.0.1:1", "GET", f"/v1/models/{i}", 200, 1.0)

    text = access_log.tail(lines=2)
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "/v1/models/4" in lines[-1]
