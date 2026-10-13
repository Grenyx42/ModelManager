import stat

from modelmanager import auth


def test_generate_token_is_long_and_random():
    a = auth.generate_token()
    b = auth.generate_token()
    assert len(a) == auth.TOKEN_BYTES * 2  # hex-encoded
    assert a != b


def test_write_then_read_token_round_trips(tmp_path):
    path = tmp_path / "token"
    token = auth.generate_token()

    written_path = auth.write_token(token, path=path)

    assert written_path == path
    assert auth.read_token(path=path) == token


def test_write_token_is_only_readable_by_owner(tmp_path):
    path = tmp_path / "token"
    auth.write_token(auth.generate_token(), path=path)

    mode = stat.S_IMODE(path.stat().st_mode)

    assert mode == 0o600


def test_read_token_returns_none_when_nothing_configured(tmp_path):
    assert auth.read_token(path=tmp_path / "does-not-exist") is None


def test_env_var_wins_over_file(tmp_path, monkeypatch):
    path = tmp_path / "token"
    auth.write_token("from-file", path=path)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "from-env")

    assert auth.read_token(path=path) == "from-env"


def test_read_token_strips_whitespace(tmp_path):
    path = tmp_path / "token"
    path.write_text("  a-token-with-padding  \n")

    assert auth.read_token(path=path) == "a-token-with-padding"


def test_default_token_path_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MODELMANAGER_TOKEN_PATH", raising=False)

    from modelmanager.config import default_token_path

    assert default_token_path() == tmp_path / "modelmanager" / "token"


def test_default_tls_paths_honor_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MODELMANAGER_TLS_CERT_PATH", raising=False)
    monkeypatch.delenv("MODELMANAGER_TLS_KEY_PATH", raising=False)

    from modelmanager.config import default_tls_cert_path, default_tls_key_path

    assert default_tls_cert_path() == tmp_path / "modelmanager" / "tls" / "cert.pem"
    assert default_tls_key_path() == tmp_path / "modelmanager" / "tls" / "key.pem"


def test_tls_path_env_overrides_win(monkeypatch, tmp_path):
    cert = tmp_path / "custom-cert.pem"
    key = tmp_path / "custom-key.pem"
    monkeypatch.setenv("MODELMANAGER_TLS_CERT_PATH", str(cert))
    monkeypatch.setenv("MODELMANAGER_TLS_KEY_PATH", str(key))

    from modelmanager.config import default_tls_cert_path, default_tls_key_path

    assert default_tls_cert_path() == cert
    assert default_tls_key_path() == key
