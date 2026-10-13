import pytest

from modelmanager import remote_paths
from modelmanager.errors import CommandFailedError
from modelmanager.runners.base import CommandResult
from modelmanager.runners.ssh import SSHRunner


class FakeSSHRunner(SSHRunner):
    def __init__(self, host_alias, home=None, ok=True):
        super().__init__(host_alias)
        self.home = home
        self.ok = ok
        self.calls = 0

    def run_shell(self, cmd, timeout=30):
        self.calls += 1
        if not self.ok:
            return CommandResult(1, "", "connection refused")
        return CommandResult(0, self.home or "", "")


def test_resolves_and_formats_paths():
    runner = FakeSSHRunner("monolith", home="C:\\Users\\wade")

    assert remote_paths.windows_home_dir(runner) == "C:\\Users\\wade"
    assert remote_paths.windows_agent_dir(runner) == "C:\\Users\\wade\\.ModelManager"
    assert remote_paths.windows_agent_script_path(runner) == "C:\\Users\\wade\\.ModelManager\\agent.ps1"


def test_caches_after_first_resolution():
    runner = FakeSSHRunner("monolith", home="C:\\Users\\wade")

    remote_paths.windows_home_dir(runner)
    remote_paths.windows_home_dir(runner)
    remote_paths.windows_agent_script_path(runner)

    assert runner.calls == 1


def test_raises_when_resolution_fails():
    runner = FakeSSHRunner("coder", ok=False)
    with pytest.raises(CommandFailedError, match="coder"):
        remote_paths.windows_home_dir(runner)


def test_raises_when_output_is_empty():
    runner = FakeSSHRunner("coder", home="")
    with pytest.raises(CommandFailedError):
        remote_paths.windows_home_dir(runner)


def test_different_hosts_cached_independently():
    runner_a = FakeSSHRunner("monolith", home="C:\\Users\\wade")
    runner_b = FakeSSHRunner("coder", home="C:\\Users\\other")

    assert remote_paths.windows_home_dir(runner_a) == "C:\\Users\\wade"
    assert remote_paths.windows_home_dir(runner_b) == "C:\\Users\\other"
