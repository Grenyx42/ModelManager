import logging

from modelmanager import access_log


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


def test_tail_respects_line_count(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    access_log.configure()

    for i in range(5):
        access_log.record("127.0.0.1:1", "GET", f"/v1/models/{i}", 200, 1.0)

    text = access_log.tail(lines=2)
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "/v1/models/4" in lines[-1]
