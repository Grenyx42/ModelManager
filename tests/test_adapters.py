import base64
import json
import textwrap

import httpx
import pytest

from modelmanager.adapters.llamacpp import LlamaCppAdapter
from modelmanager.adapters.ollama import OllamaAdapter
from modelmanager.config import Candidate, EngineConfig, SystemConfig
from modelmanager.runners.base import CommandResult
from modelmanager.runners.local import LocalRunner
from modelmanager.runners.ssh import SSHRunner


def fake_binary(tmp_path):
    script = tmp_path / "fake-llama-server"
    script.write_text(textwrap.dedent("""\
        #!/bin/sh
        sleep 5
    """))
    script.chmod(0o755)
    return str(script)


def test_llamacpp_local_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = EngineConfig(system="sleek", type="llama.cpp", binary=fake_binary(tmp_path))
    system = SystemConfig(os="linux", control="local")
    adapter = LlamaCppAdapter(engine, system, LocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path="/models/x.gguf")

    adapter.start("qwen-72b", candidate)
    status = adapter.status("qwen-72b", candidate)
    assert status.running is True
    assert status.pid is not None

    adapter.stop("qwen-72b", candidate)
    status = adapter.status("qwen-72b", candidate)
    assert status.running is False


class FakeSSHRunner(SSHRunner):
    def __init__(self):
        super().__init__("monolith")
        self.calls = []

    def run_shell(self, cmd, timeout=30):
        self.calls.append(cmd)
        if "-Action start" in cmd:
            return CommandResult(0, json.dumps({"pid": 4242, "log_path": "C:\\ModelManager\\logs\\x.log"}), "")
        if "-Action stop" in cmd:
            return CommandResult(0, json.dumps({"status": "stopped"}), "")
        if "-Action status" in cmd:
            return CommandResult(0, json.dumps({"running": True, "pid": 4242}), "")
        if "-Action logs" in cmd:
            return CommandResult(0, "log line 1\nlog line 2\n", "")
        return CommandResult(1, "", "unknown action")


def test_llamacpp_ssh_lifecycle_invokes_agent_script():
    engine = EngineConfig(system="monolith", type="llama.cpp", binary=r"C:\engines\buildB\llama-server.exe")
    system = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    runner = FakeSSHRunner()
    adapter = LlamaCppAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path=r"D:\models\x.gguf")

    adapter.start("qwen-72b", candidate)
    status = adapter.status("qwen-72b", candidate)
    assert status.running is True
    assert status.pid == 4242

    logs = adapter.logs("qwen-72b", candidate)
    assert "log line 1" in logs

    adapter.stop("qwen-72b", candidate)

    start_call = runner.calls[0]
    payload_b64 = start_call.split("-PayloadB64 ")[1]
    payload = json.loads(base64.b64decode(payload_b64))
    assert payload["model_path"] == r"D:\models\x.gguf"
    assert payload["port"] == 8081


class FakeLocalRunner(LocalRunner):
    def __init__(self):
        self.shell_calls = []

    def run_shell(self, cmd, timeout=30):
        self.shell_calls.append(cmd)
        return CommandResult(1, "", "no journal")


def test_ollama_status_and_lifecycle(monkeypatch):
    engine = EngineConfig(system="sleek", type="ollama", base_url="http://localhost:11434")
    adapter = OllamaAdapter(engine, FakeLocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text", ollama_task="embed")

    posted = {}

    def fake_post(url, json, timeout):
        posted["url"] = url
        posted["json"] = json
        return httpx.Response(200, json={"done": True}, request=httpx.Request("POST", url))

    def fake_get(url, timeout):
        return httpx.Response(
            200, json={"models": [{"name": "nomic-embed-text"}]}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    adapter.start("nomic-embed", candidate)
    assert posted["json"]["keep_alive"] == -1
    assert posted["url"].endswith("/api/embed")

    status = adapter.status("nomic-embed", candidate)
    assert status.running is True

    adapter.stop("nomic-embed", candidate)
    assert posted["json"]["keep_alive"] == 0


def test_ollama_status_when_unreachable(monkeypatch):
    engine = EngineConfig(system="sleek", type="ollama", base_url="http://localhost:11434")
    adapter = OllamaAdapter(engine, FakeLocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    def fake_get(url, timeout):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    status = adapter.status("nomic-embed", candidate)
    assert status.running is False
