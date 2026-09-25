from modelmanager import agent_deploy
from modelmanager.config import Registry, SystemConfig
from modelmanager.runners.base import CommandResult
from modelmanager.runners.ssh import SSHRunner


class FakeSSHRunner(SSHRunner):
    def __init__(self, remote_hash: str | None):
        super().__init__("monolith")
        self.remote_hash = remote_hash
        self.shell_calls: list[str] = []
        self.uploads: list[tuple] = []
        self.mkdir_ok = True
        self.upload_ok = True

    def run_shell(self, cmd, timeout=30):
        self.shell_calls.append(cmd)
        if "$env:USERPROFILE" in cmd:
            return CommandResult(0, "C:\\Users\\wade", "")
        if "Get-FileHash" in cmd:
            if self.remote_hash is None:
                return CommandResult(0, "", "")
            return CommandResult(0, self.remote_hash.upper(), "")
        if "New-Item" in cmd:
            return CommandResult(0 if self.mkdir_ok else 1, "", "" if self.mkdir_ok else "denied")
        return CommandResult(1, "", "unexpected command")

    def upload_file(self, local_path, remote_path, timeout=30):
        self.uploads.append((local_path, remote_path))
        return CommandResult(0 if self.upload_ok else 1, "", "" if self.upload_ok else "scp failed")


def windows_ssh_system() -> SystemConfig:
    return SystemConfig(os="windows", control="ssh", ssh_host="monolith")


def test_skips_non_windows_or_local_systems():
    linux_local = SystemConfig(os="linux", control="local")
    result = agent_deploy.deploy_if_needed("sleek", linux_local)
    assert result["action"] == "skipped"


def test_unchanged_when_remote_hash_matches(monkeypatch):
    local_hash = agent_deploy.local_agent_hash()
    runner = FakeSSHRunner(remote_hash=local_hash)
    monkeypatch.setattr(agent_deploy, "SSHRunner", lambda host: runner)

    result = agent_deploy.deploy_if_needed("monolith", windows_ssh_system())

    assert result == {"system": "monolith", "action": "unchanged", "hash": local_hash}
    assert runner.uploads == []


def test_deploys_when_remote_hash_differs(monkeypatch):
    runner = FakeSSHRunner(remote_hash="deadbeef")
    monkeypatch.setattr(agent_deploy, "SSHRunner", lambda host: runner)

    result = agent_deploy.deploy_if_needed("monolith", windows_ssh_system())

    assert result["action"] == "deployed"
    assert result["path"] == "C:\\Users\\wade\\.ModelManager\\agent.ps1"
    assert len(runner.uploads) == 1
    assert runner.uploads[0][0] == agent_deploy.LOCAL_AGENT_SCRIPT_PATH
    assert runner.uploads[0][1] == "C:/Users/wade/.ModelManager/agent.ps1"
    assert any("C:\\Users\\wade\\.ModelManager\\pids" in c for c in runner.shell_calls)


def test_deploys_when_remote_file_missing(monkeypatch):
    runner = FakeSSHRunner(remote_hash=None)
    monkeypatch.setattr(agent_deploy, "SSHRunner", lambda host: runner)

    result = agent_deploy.deploy_if_needed("monolith", windows_ssh_system())

    assert result["action"] == "deployed"


def test_force_skips_hash_check(monkeypatch):
    local_hash = agent_deploy.local_agent_hash()
    runner = FakeSSHRunner(remote_hash=local_hash)
    monkeypatch.setattr(agent_deploy, "SSHRunner", lambda host: runner)

    result = agent_deploy.deploy_if_needed("monolith", windows_ssh_system(), force=True)

    assert result["action"] == "deployed"
    assert not any("Get-FileHash" in c for c in runner.shell_calls)


def test_raises_when_upload_fails(monkeypatch):
    runner = FakeSSHRunner(remote_hash="deadbeef")
    runner.upload_ok = False
    monkeypatch.setattr(agent_deploy, "SSHRunner", lambda host: runner)

    try:
        agent_deploy.deploy_if_needed("monolith", windows_ssh_system())
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "monolith" in str(exc)


def test_deploy_all_skips_local_and_reports_failures(monkeypatch):
    calls = {}

    def fake_deploy(name, system, force=False):
        calls[name] = system
        if name == "coder":
            raise RuntimeError("offline")
        return {"system": name, "action": "deployed", "hash": "abc"}

    monkeypatch.setattr(agent_deploy, "deploy_if_needed", fake_deploy)

    registry = Registry(
        systems={
            "sleek": SystemConfig(os="linux", control="local"),
            "monolith": windows_ssh_system(),
            "coder": SystemConfig(os="windows", control="ssh", ssh_host="coder"),
        },
        gpu_groups={},
        engines={},
        models={},
    )

    results = agent_deploy.deploy_all(registry)

    assert "sleek" not in calls
    by_name = {r["system"]: r for r in results}
    assert by_name["monolith"]["action"] == "deployed"
    assert by_name["coder"]["action"] == "failed"
    assert "offline" in by_name["coder"]["error"]
