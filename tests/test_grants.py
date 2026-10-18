import stat

from modelmanager import grants


def test_add_then_find_grant_by_token_round_trips(tmp_path):
    path = tmp_path / "grants.json"

    grants.add_grant("bob", "bobs-token", ["sys-a"], path=path)

    found = grants.find_grant_by_token("bobs-token", path=path)
    assert found is not None
    assert found["name"] == "bob"
    assert found["allowed_systems"] == ["sys-a"]


def test_find_grant_by_token_returns_none_for_unknown_token(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", ["sys-a"], path=path)

    assert grants.find_grant_by_token("not-a-real-token", path=path) is None


def test_grants_file_never_stores_the_raw_token(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "super-secret-token", ["sys-a"], path=path)

    assert "super-secret-token" not in path.read_text()


def test_add_grant_overwrites_existing_name(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "old-token", ["sys-a"], path=path)
    grants.add_grant("bob", "new-token", ["sys-b"], path=path)

    entries = grants.load_grants(path)
    assert len(entries) == 1
    assert entries[0]["allowed_systems"] == ["sys-b"]
    assert grants.find_grant_by_token("old-token", path=path) is None
    assert grants.find_grant_by_token("new-token", path=path)["name"] == "bob"


def test_remove_grant(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", ["sys-a"], path=path)

    assert grants.remove_grant("bob", path=path) is True
    assert grants.load_grants(path) == []
    assert grants.remove_grant("bob", path=path) is False


def test_load_grants_returns_empty_list_when_file_missing(tmp_path):
    assert grants.load_grants(tmp_path / "does-not-exist.json") == []


def test_grants_file_is_only_readable_by_owner(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", ["sys-a"], path=path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_default_grants_path_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MODELMANAGER_GRANTS_PATH", raising=False)

    from modelmanager.config import default_grants_path

    assert default_grants_path() == tmp_path / "modelmanager" / "grants.json"
