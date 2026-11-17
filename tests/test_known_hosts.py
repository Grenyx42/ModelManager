from modelmanager import known_hosts


def test_add_then_get_known_host_round_trips(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"

    known_hosts.add_known_host("alice", "https://systemA:8778", "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n", "AB:CD", path=path, cert_dir=cert_dir)

    entry = known_hosts.get_known_host("alice", path=path)
    assert entry is not None
    assert entry["url"] == "https://systemA:8778"
    assert entry["fingerprint"] == "AB:CD"
    assert entry["cert_file"] == str(cert_dir / "alice.pem")


def test_add_known_host_writes_the_cert_pem_file(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"
    pem = "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"

    known_hosts.add_known_host("alice", "https://systemA:8778", pem, "AB:CD", path=path, cert_dir=cert_dir)

    assert (cert_dir / "alice.pem").read_text() == pem


def test_get_known_host_returns_none_for_unknown_name(tmp_path):
    assert known_hosts.get_known_host("nobody", path=tmp_path / "known_hosts.json") is None


def test_add_known_host_overwrites_existing_name(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"
    known_hosts.add_known_host("alice", "https://old:8778", "old-pem", "11:11", path=path, cert_dir=cert_dir)
    known_hosts.add_known_host("alice", "https://new:8778", "new-pem", "22:22", path=path, cert_dir=cert_dir)

    entries = known_hosts.load_known_hosts(path)
    assert len(entries) == 1
    assert entries["alice"]["url"] == "https://new:8778"
    assert (cert_dir / "alice.pem").read_text() == "new-pem"


def test_add_known_host_sets_last_connected_to_added_at(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"

    entry = known_hosts.add_known_host("alice", "https://systemA:8778", "pem", "AB:CD", path=path, cert_dir=cert_dir)

    assert entry["last_connected"] == entry["added_at"]


def test_touch_known_host_bumps_last_connected(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"
    added = known_hosts.add_known_host("alice", "https://systemA:8778", "pem", "AB:CD", path=path, cert_dir=cert_dir)

    touched = known_hosts.touch_known_host("alice", path=path)

    assert touched is not None
    assert touched["last_connected"] >= added["last_connected"]
    assert known_hosts.get_known_host("alice", path=path)["last_connected"] == touched["last_connected"]


def test_touch_known_host_returns_none_for_unknown_name(tmp_path):
    assert known_hosts.touch_known_host("nobody", path=tmp_path / "known_hosts.json") is None


def test_remove_known_host_deletes_entry_and_cert_file(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"
    known_hosts.add_known_host("alice", "https://systemA:8778", "pem", "AB:CD", path=path, cert_dir=cert_dir)

    assert known_hosts.remove_known_host("alice", path=path) is True
    assert known_hosts.load_known_hosts(path) == {}
    assert not (cert_dir / "alice.pem").exists()
    assert known_hosts.remove_known_host("alice", path=path) is False


def test_load_known_hosts_returns_empty_dict_when_file_missing(tmp_path):
    assert known_hosts.load_known_hosts(tmp_path / "does-not-exist.json") == {}


# --- per-peer token: generated once, preserved on re-add, rotated on demand

def test_add_known_host_generates_a_token(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"

    entry = known_hosts.add_known_host("alice", "https://systemA:8778", "pem", "AB:CD", path=path, cert_dir=cert_dir)

    assert entry["token"]
    assert isinstance(entry["token"], str)


def test_two_different_hosts_get_two_different_tokens(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"

    alice = known_hosts.add_known_host("alice", "https://systemA:8778", "pem", "AB:CD", path=path, cert_dir=cert_dir)
    bob = known_hosts.add_known_host("bob", "https://systemB:8778", "pem", "EF:01", path=path, cert_dir=cert_dir)

    # The whole point of per-peer tokens: a captured one must only work
    # against the single peer it was generated for.
    assert alice["token"] != bob["token"]


def test_re_adding_an_existing_name_preserves_its_token(tmp_path):
    # Re-trusting a rotated cert shouldn't also silently invalidate the
    # credential already shared with that peer's owner for `grant add`.
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"
    first = known_hosts.add_known_host("alice", "https://old:8778", "old-pem", "11:11", path=path, cert_dir=cert_dir)

    second = known_hosts.add_known_host("alice", "https://new:8778", "new-pem", "22:22", path=path, cert_dir=cert_dir)

    assert second["token"] == first["token"]
    assert second["url"] == "https://new:8778"


def test_rotate_host_token_changes_only_the_token(tmp_path):
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"
    original = known_hosts.add_known_host("alice", "https://systemA:8778", "pem", "AB:CD", path=path, cert_dir=cert_dir)

    rotated = known_hosts.rotate_host_token("alice", path=path)

    assert rotated["token"] != original["token"]
    assert rotated["url"] == original["url"]
    assert rotated["fingerprint"] == original["fingerprint"]


def test_rotate_host_token_returns_none_for_unknown_name(tmp_path):
    assert known_hosts.rotate_host_token("nobody", path=tmp_path / "known_hosts.json") is None


def test_known_hosts_file_is_only_readable_by_owner(tmp_path):
    import stat
    path = tmp_path / "known_hosts.json"
    cert_dir = tmp_path / "certs"
    known_hosts.add_known_host("alice", "https://systemA:8778", "pem", "AB:CD", path=path, cert_dir=cert_dir)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_default_known_hosts_paths_honor_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MODELMANAGER_KNOWN_HOSTS_PATH", raising=False)
    monkeypatch.delenv("MODELMANAGER_KNOWN_HOSTS_DIR", raising=False)

    from modelmanager.config import default_known_hosts_dir, default_known_hosts_path

    assert default_known_hosts_path() == tmp_path / "modelmanager" / "known_hosts.json"
    assert default_known_hosts_dir() == tmp_path / "modelmanager" / "known_hosts"
