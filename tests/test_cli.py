import json
from types import SimpleNamespace

import pytest

from modelmanager import cli, known_hosts


# --- _client: never send a bearer token over a non-https url ------------

def test_client_refuses_non_https_url_with_token(monkeypatch):
    # `hosts add` already refuses a non-https peer (CWE-319), but that
    # only protects the --peer path - a bare
    # --url http://... (or MODELMANAGER_URL) bypasses known_hosts entirely
    # and would otherwise send Authorization: Bearer in cleartext.
    monkeypatch.setattr(cli.auth, "read_token", lambda: None)
    args = SimpleNamespace(url="http://evil-coordinator.example:8778", token="tok123", peer=None)
    with pytest.raises(SystemExit):
        cli._client(args)


def test_client_allows_non_https_url_without_token(monkeypatch):
    monkeypatch.setattr(cli.auth, "read_token", lambda: None)
    args = SimpleNamespace(url="http://localhost:8778", token=None, peer=None)
    client = cli._client(args)
    client.close()


# --- _client: --peer must never send the owner token --------------------

def _known_host_entry(tmp_path, token="peer-token-value"):
    # cert_file deliberately doesn't exist - _client() falls back to the
    # system CA bundle (verify=True) in that case, which is all these tests
    # care about; a real PEM isn't needed to test token selection.
    cert_path = tmp_path / "peer.pem"
    entry = {"url": "https://peerbox:8778", "cert_file": str(cert_path)}
    if token is not None:
        entry["token"] = token
    return entry


def test_client_sends_per_peer_token_not_owner_token_for_peer(monkeypatch, tmp_path):
    # The whole point of the owner/per-peer split: a --peer coordinator
    # isn't this machine, so even a fully malicious/compromised one must
    # never see this coordinator's own owner token (see auth.read_token's
    # own docstring on why that would be unrestricted access if replayed
    # back).
    monkeypatch.setattr(cli.known_hosts, "get_known_host", lambda name: _known_host_entry(tmp_path))
    monkeypatch.setattr(cli.auth, "read_token", lambda: "owner-token-value")
    args = SimpleNamespace(url=None, token=None, peer="alice")

    client = cli._client(args)

    assert client.headers["authorization"] == "Bearer peer-token-value"
    client.close()


def test_client_refuses_peer_request_with_no_token_saved_for_that_peer(monkeypatch, tmp_path):
    # Must fail closed, not silently fall back to the owner token - that
    # fallback is exactly the vulnerability this split fixes. Only reachable
    # for a known_hosts.json entry saved before per-peer tokens existed.
    monkeypatch.setattr(cli.known_hosts, "get_known_host", lambda name: _known_host_entry(tmp_path, token=None))
    monkeypatch.setattr(cli.auth, "read_token", lambda: "owner-token-value")
    args = SimpleNamespace(url=None, token=None, peer="alice")

    with pytest.raises(SystemExit):
        cli._client(args)


def test_client_explicit_token_flag_wins_for_peer_too(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.known_hosts, "get_known_host", lambda name: _known_host_entry(tmp_path))
    args = SimpleNamespace(url=None, token="explicit-override", peer="alice")

    client = cli._client(args)

    assert client.headers["authorization"] == "Bearer explicit-override"
    client.close()


def test_client_uses_the_right_peers_token_not_another_peers(monkeypatch, tmp_path):
    # Each known_hosts entry carries its own token - a captured one must
    # only ever be sent to the peer it was generated for.
    entries = {
        "alice": _known_host_entry(tmp_path, token="alices-token"),
        "bob": _known_host_entry(tmp_path, token="bobs-token"),
    }
    monkeypatch.setattr(cli.known_hosts, "get_known_host", lambda name: entries[name])
    args = SimpleNamespace(url=None, token=None, peer="bob")

    client = cli._client(args)

    assert client.headers["authorization"] == "Bearer bobs-token"
    client.close()


def test_client_local_url_still_uses_owner_token(monkeypatch):
    # No --peer: this is the "second machine you yourself own" path
    # (docs/deployment.md) - it's correct for this to keep using the owner
    # token, since both ends are genuinely you.
    monkeypatch.setattr(cli.auth, "read_token", lambda: "owner-token-value")
    args = SimpleNamespace(url="https://127.0.0.1:8778", token=None, peer=None)

    client = cli._client(args)

    assert client.headers["authorization"] == "Bearer owner-token-value"
    client.close()


# --- _validate_ssh_host / ssh-argument-injection guard -----------------

def test_validate_ssh_host_accepts_plain_alias():
    assert cli._validate_ssh_host("monolith") == "monolith"
    assert cli._validate_ssh_host("my-host.example.com") == "my-host.example.com"


@pytest.mark.parametrize("host", [
    "alice@worker", "2001:db8::10", "[2001:db8::10]", "user@2001:db8::1", "_gpu", "_service@worker",
    "fe80::1%eth0", "[fe80::1%eth0]", "alice@fe80::1%eth0", r"CONTOSO\alice@worker",
    "fe80::1%br-lan", "fe80::1%enp3s0.100",
])
def test_validate_ssh_host_accepts_user_prefix_and_ipv6(host):
    # SystemConfig.ssh_host (config.py) is
    # documented as a plain "SSH alias/hostname" with no further constraint,
    # and the registry already accepts user@host and bare-IPv6 values - the
    # original bare-hostname-only regex would have silently broken `logs
    # --follow` on an otherwise-valid system instead of catching anything
    # unsafe.
    assert cli._validate_ssh_host(host) == host


@pytest.mark.parametrize("host", [
    "-oProxyCommand=evil", "--", "", "host;rm -rf /", "foo@-oProxyCommand=evil", "-alice@worker",
    "-CONTOSO\\alice@worker", "[2001:db8::1", "2001:db8::1]",
])
def test_validate_ssh_host_rejects_unsafe_values(host):
    # Confirmed in review: ssh/scp treat a destination starting with "-" as
    # an option rather than a hostname, so a peer-controlled ssh_host like
    # "-oProxyCommand=..." would otherwise get arbitrary local execution.
    # The user@ prefix is held to the same rule - "foo@-oProxyCommand=evil"
    # doesn't start with "-" itself, but the host portion after "@" must
    # still look like a safe destination on its own. The mismatched-bracket
    # cases aren't an injection path, just a malformed IPv6 literal that
    # should fail validation cleanly instead of sailing through.
    with pytest.raises(ValueError):
        cli._validate_ssh_host(host)


@pytest.mark.parametrize("url", ["https://127.0.0.1:8778", "https://localhost:8778", "https://[::1]:8778"])
def test_is_loopback_url_accepts_loopback_forms(url):
    assert cli._is_loopback_url(url) is True


@pytest.mark.parametrize("url", ["https://sleek:8778", "https://evil-coordinator.example:8778", "https://100.120.83.74:8778"])
def test_is_loopback_url_rejects_non_loopback(url):
    assert cli._is_loopback_url(url) is False


def test_follow_command_raises_for_unsafe_ssh_host():
    info = {"control": "ssh", "os": "linux", "ssh_host": "-oProxyCommand=evil", "paths": ["/var/log/x.log"]}
    with pytest.raises(ValueError):
        cli._follow_command(info, 100)


def test_follow_command_builds_ssh_tail_for_safe_host():
    info = {"control": "ssh", "os": "linux", "ssh_host": "monolith", "paths": ["/var/log/x.log"]}
    assert cli._follow_command(info, 100) == ["ssh", "monolith", "tail", "-f", "-n", "100", "/var/log/x.log"]


def test_windows_tail_command_raises_for_unsafe_ssh_host():
    with pytest.raises(ValueError):
        cli._windows_tail_command("-oProxyCommand=evil", "C:\\log.txt", 100)


# --- cmd_logs: peer targets never build a direct ssh/powershell command --

class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, *args, **kwargs):
        return self._response


def _patch_client(monkeypatch, info):
    monkeypatch.setattr(cli, "_client", lambda args, timeout=30: _FakeClient(_FakeResponse(200, info)))


def test_cmd_logs_follow_on_peer_never_builds_direct_ssh_command(monkeypatch, capsys):
    # The log-path response for an active --peer is that peer's own
    # coordinator's data, not this user's - even though it claims "control":
    # "ssh", a malicious ssh_host here must never reach the local ssh binary.
    info = {"control": "ssh", "os": "linux", "ssh_host": "-oProxyCommand=evil", "paths": ["/var/log/x.log"]}
    _patch_client(monkeypatch, info)

    captured = {}

    def fake_run(command, env=None):
        captured["command"] = command
        captured["env"] = env
        return SimpleNamespace(returncode=0)

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    args = SimpleNamespace(name="text", lines=100, follow=True, peer="some-peer", url="https://x", token="tok123")
    rc = cli.cmd_logs(args)

    assert rc == 0
    assert captured["command"][0] == "watch"
    assert "-x" in captured["command"]
    assert "-oProxyCommand=evil" not in captured["command"]
    # Without re-passing --peer/--url, each 2s poll would silently fall
    # back to the default coordinator instead of staying on the one the
    # user actually targeted.
    assert captured["command"] == [
        "watch", "-x", "-t", "-n", "2", cli.sys.argv[0],
        "--url", "https://x", "--peer", "some-peer",
        "logs", "text", "--lines", "100",
    ]
    # --token must never ride along in argv (watch
    # prints its own command in the header, and ps shows argv regardless of
    # -t) - it has to reach the re-invoked process via env instead.
    assert captured["env"]["MODELMANAGER_TOKEN"] == "tok123"
    assert "tok123" not in captured["command"]


def test_cmd_logs_follow_local_with_unsafe_ssh_host_is_refused(monkeypatch, capsys):
    info = {"control": "ssh", "os": "linux", "ssh_host": "-oProxyCommand=evil", "paths": ["/var/log/x.log"]}
    _patch_client(monkeypatch, info)

    import subprocess
    called = {"run": False}

    def fake_run(command, env=None):
        called["run"] = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    args = SimpleNamespace(name="text", lines=100, follow=True, peer=None, url="https://127.0.0.1:8778", token=None)
    rc = cli.cmd_logs(args)

    assert rc == 1
    assert called["run"] is False
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_cmd_logs_follow_on_bare_remote_url_without_peer_never_builds_direct_ssh_command(monkeypatch):
    # Gating "is this local" on --peer
    # alone misses a bare --url pointed straight at a remote coordinator
    # (no --peer, no known_hosts entry at all) - that coordinator's
    # log-path response is exactly as untrusted as a --peer's, and must
    # fall through to the same REST-polling fallback instead of driving a
    # local ssh command.
    info = {"control": "ssh", "os": "linux", "ssh_host": "-oProxyCommand=evil", "paths": ["/var/log/x.log"]}
    _patch_client(monkeypatch, info)

    captured = {}

    def fake_run(command, env=None):
        captured["command"] = command
        return SimpleNamespace(returncode=0)

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    args = SimpleNamespace(name="text", lines=100, follow=True, peer=None, url="https://evil-coordinator.example:8778", token=None)
    rc = cli.cmd_logs(args)

    assert rc == 0
    assert captured["command"][0] == "watch"
    assert "-oProxyCommand=evil" not in captured["command"]


# --- hosts add: --expect-fingerprint pinning ----------------------------

def _hosts_add_args(**overrides):
    base = dict(
        hosts_action="add", name="peerbox", url="https://peerbox:8778",
        yes=True, expect_fingerprint=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_hosts_add_rejects_non_https_url(monkeypatch):
    # _client() sends the saved url as base_url with Authorization:
    # Bearer <token> on every request (CWE-319) - an
    # http:// entry would leak that token in cleartext to any on-path
    # observer. Must be refused before even probing the cert.
    probe_called = []
    monkeypatch.setattr(cli, "_fetch_cert_fingerprint", lambda url: probe_called.append(url) or ("PEM", "AA:BB"))
    add_called = []
    monkeypatch.setattr(known_hosts, "add_known_host", lambda *a, **k: add_called.append(a))

    rc = cli.cmd_hosts(_hosts_add_args(url="http://peerbox:8778", expect_fingerprint="AA:BB"))

    assert rc == 1
    assert probe_called == []
    assert add_called == []


def test_hosts_add_with_yes_requires_expect_fingerprint(monkeypatch):
    monkeypatch.setattr(cli, "_fetch_cert_fingerprint", lambda url: ("PEM", "AA:BB"))
    add_called = []
    monkeypatch.setattr(known_hosts, "add_known_host", lambda *a, **k: add_called.append(a))

    rc = cli.cmd_hosts(_hosts_add_args(expect_fingerprint=None))

    assert rc == 1
    assert add_called == []


def test_hosts_add_rejects_mismatched_fingerprint(monkeypatch):
    # The fingerprint shown during `hosts probe` no longer matches what this
    # (separate) connection just fetched - exactly the gap an interceptor
    # that only swaps the second connection's cert would exploit.
    monkeypatch.setattr(cli, "_fetch_cert_fingerprint", lambda url: ("PEM", "AA:BB"))
    add_called = []
    monkeypatch.setattr(known_hosts, "add_known_host", lambda *a, **k: add_called.append(a))

    rc = cli.cmd_hosts(_hosts_add_args(expect_fingerprint="FF:EE"))

    assert rc == 1
    assert add_called == []


def test_hosts_add_accepts_matching_fingerprint(monkeypatch):
    monkeypatch.setattr(cli, "_fetch_cert_fingerprint", lambda url: ("PEM", "AA:BB"))
    add_called = []
    monkeypatch.setattr(
        known_hosts, "add_known_host",
        lambda name, url, pem, fingerprint: add_called.append((name, url, pem, fingerprint)) or {
            "url": url, "fingerprint": fingerprint, "token": "peer-token-value", "token_regenerated": False,
        },
    )

    rc = cli.cmd_hosts(_hosts_add_args(expect_fingerprint="AA:BB"))

    assert rc == 0
    assert add_called == [("peerbox", "https://peerbox:8778", "PEM", "AA:BB")]


def test_hosts_add_prints_the_peer_token(monkeypatch, capsys):
    # The caller needs this to actually hand to the peer's owner for their
    # `grant add` - it has to be in the result, not just saved silently.
    monkeypatch.setattr(cli, "_fetch_cert_fingerprint", lambda url: ("PEM", "AA:BB"))
    monkeypatch.setattr(
        known_hosts, "add_known_host",
        lambda name, url, pem, fingerprint: {
            "url": url, "fingerprint": fingerprint, "token": "peer-token-value", "token_regenerated": False,
        },
    )

    rc = cli.cmd_hosts(_hosts_add_args(expect_fingerprint="AA:BB"))

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["token"] == "peer-token-value"
    assert "note" not in out


def test_hosts_add_flags_and_explains_a_regenerated_token(monkeypatch, capsys):
    # A fingerprint change under the same name means a different identity -
    # the caller must be told the token is new and needs re-sharing, not
    # just silently handed a value that happens to differ from last time.
    monkeypatch.setattr(cli, "_fetch_cert_fingerprint", lambda url: ("PEM", "AA:BB"))
    monkeypatch.setattr(
        known_hosts, "add_known_host",
        lambda name, url, pem, fingerprint: {
            "url": url, "fingerprint": fingerprint, "token": "new-token-value", "token_regenerated": True,
        },
    )

    rc = cli.cmd_hosts(_hosts_add_args(expect_fingerprint="AA:BB"))

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["token"] == "new-token-value"
    assert out["token_regenerated"] is True
    assert "note" in out


# --- hosts rotate-token --------------------------------------------------

def test_hosts_rotate_token_prints_the_new_token(monkeypatch, capsys):
    monkeypatch.setattr(known_hosts, "rotate_host_token", lambda name: {"token": "new-token-value"})

    rc = cli.cmd_hosts(SimpleNamespace(hosts_action="rotate-token", name="peerbox"))

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"name": "peerbox", "token": "new-token-value"}


def test_hosts_rotate_token_fails_for_unknown_host(monkeypatch, capsys):
    monkeypatch.setattr(known_hosts, "rotate_host_token", lambda name: None)

    rc = cli.cmd_hosts(SimpleNamespace(hosts_action="rotate-token", name="nobody"))

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out
