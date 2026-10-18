import stat

from modelmanager import grants


def test_add_grant_starts_with_no_allowed_systems(tmp_path):
    path = tmp_path / "grants.json"

    entry = grants.add_grant("bob", "bobs-token", path=path)

    assert entry["allowed_systems"] == []
    found = grants.find_grant_by_token("bobs-token", path=path)
    assert found is not None
    assert found["name"] == "bob"


def test_find_grant_by_token_returns_none_for_unknown_token(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    assert grants.find_grant_by_token("not-a-real-token", path=path) is None


def test_grants_file_never_stores_the_raw_token(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "super-secret-token", path=path)

    assert "super-secret-token" not in path.read_text()


def test_allow_system_adds_to_allowed_systems(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    entry = grants.allow_system("bob", "sys-a", path=path)

    assert entry["allowed_systems"] == ["sys-a"]
    assert grants.find_grant_by_token("bobs-token", path=path)["allowed_systems"] == ["sys-a"]


def test_allow_system_is_idempotent(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)
    grants.allow_system("bob", "sys-a", path=path)

    entry = grants.allow_system("bob", "sys-a", path=path)

    assert entry["allowed_systems"] == ["sys-a"]


def test_allow_system_returns_none_for_unknown_grant(tmp_path):
    assert grants.allow_system("nobody", "sys-a", path=tmp_path / "grants.json") is None


def test_revoke_system_removes_from_allowed_systems(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)
    grants.allow_system("bob", "sys-a", path=path)
    grants.allow_system("bob", "sys-b", path=path)

    entry = grants.revoke_system("bob", "sys-a", path=path)

    assert entry["allowed_systems"] == ["sys-b"]


def test_revoke_system_is_idempotent(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    entry = grants.revoke_system("bob", "sys-a", path=path)

    assert entry["allowed_systems"] == []


def test_revoke_system_returns_none_for_unknown_grant(tmp_path):
    assert grants.revoke_system("nobody", "sys-a", path=tmp_path / "grants.json") is None


def test_add_grant_on_existing_name_rotates_token_but_keeps_allowed_systems(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "old-token", path=path)
    grants.allow_system("bob", "sys-a", path=path)

    entry = grants.add_grant("bob", "new-token", path=path)

    assert entry["allowed_systems"] == ["sys-a"]
    assert grants.find_grant_by_token("old-token", path=path) is None
    found = grants.find_grant_by_token("new-token", path=path)
    assert found["name"] == "bob"
    assert found["allowed_systems"] == ["sys-a"]


def test_add_grant_on_existing_name_preserves_created_at(tmp_path):
    path = tmp_path / "grants.json"
    first = grants.add_grant("bob", "old-token", path=path)

    second = grants.add_grant("bob", "new-token", path=path)

    assert second["created_at"] == first["created_at"]


def test_remove_grant(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    assert grants.remove_grant("bob", path=path) is True
    assert grants.load_grants(path) == []
    assert grants.remove_grant("bob", path=path) is False


def test_load_grants_returns_empty_list_when_file_missing(tmp_path):
    assert grants.load_grants(tmp_path / "does-not-exist.json") == []


def test_grants_file_is_only_readable_by_owner(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_default_grants_path_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MODELMANAGER_GRANTS_PATH", raising=False)

    from modelmanager.config import default_grants_path

    assert default_grants_path() == tmp_path / "modelmanager" / "grants.json"
