import stat
from datetime import datetime, timedelta, timezone

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


# --- per-system expiry ---------------------------------------------------

def _future(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past(seconds=1):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_allow_system_with_expires_at_is_effective_until_it_passes(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    entry = grants.allow_system("bob", "sys-a", path=path, expires_at=_future())

    assert grants.effective_allowed_systems(entry) == {"sys-a"}


def test_allow_system_with_a_past_expires_at_is_not_effective(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    entry = grants.allow_system("bob", "sys-a", path=path, expires_at=_past())

    assert grants.effective_allowed_systems(entry) == set()
    # Still listed in allowed_systems - expiry filters it at request time,
    # it doesn't silently rewrite the stored list.
    assert "sys-a" in entry["allowed_systems"]


def test_allow_system_without_expires_at_never_expires(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)

    entry = grants.allow_system("bob", "sys-a", path=path)

    assert grants.effective_allowed_systems(entry) == {"sys-a"}


def test_re_allowing_a_system_without_expires_at_clears_a_previous_expiry(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)
    grants.allow_system("bob", "sys-a", path=path, expires_at=_past())

    entry = grants.allow_system("bob", "sys-a", path=path)

    assert grants.effective_allowed_systems(entry) == {"sys-a"}
    assert "sys-a" not in entry["system_expirations"]


def test_revoke_system_also_clears_its_expiration(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)
    grants.allow_system("bob", "sys-a", path=path, expires_at=_future())

    entry = grants.revoke_system("bob", "sys-a", path=path)

    assert "sys-a" not in entry["system_expirations"]


def test_add_grant_preserves_system_expirations_on_rotation(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "old-token", path=path)
    grants.allow_system("bob", "sys-a", path=path, expires_at=_future())

    entry = grants.add_grant("bob", "new-token", path=path)

    assert grants.effective_allowed_systems(entry) == {"sys-a"}


def test_effective_allowed_systems_fails_closed_on_malformed_timestamp(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)
    entry = grants.allow_system("bob", "sys-a", path=path, expires_at=_future())
    entry["system_expirations"]["sys-a"] = "not-a-real-timestamp"

    assert grants.effective_allowed_systems(entry) == set()


def test_effective_allowed_systems_treats_missing_field_as_never_expiring(tmp_path):
    # A grants.json written before system_expirations existed.
    entry = {"name": "bob", "allowed_systems": ["sys-a"]}

    assert grants.effective_allowed_systems(entry) == {"sys-a"}


# --- revoke_all panic button ----------------------------------------------

def test_revoke_all_clears_access_but_keeps_identities(tmp_path):
    path = tmp_path / "grants.json"
    grants.add_grant("bob", "bobs-token", path=path)
    grants.allow_system("bob", "sys-a", path=path, expires_at=_future())
    grants.add_grant("carol", "carols-token", path=path)
    grants.allow_system("carol", "sys-b", path=path)

    entries = grants.revoke_all(path=path)

    assert {e["name"] for e in entries} == {"bob", "carol"}
    for e in entries:
        assert e["allowed_systems"] == []
        assert e["system_expirations"] == {}
    # Identities/tokens survive - re-granting later doesn't need the token
    # re-exchanged.
    assert grants.find_grant_by_token("bobs-token", path=path) is not None
    assert grants.find_grant_by_token("carols-token", path=path) is not None


def test_revoke_all_on_empty_grants_is_a_no_op(tmp_path):
    assert grants.revoke_all(path=tmp_path / "grants.json") == []
