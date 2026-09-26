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


def test_llamacpp_local_start_passes_engine_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class RecordingLocalRunner(LocalRunner):
        def __init__(self):
            self.calls = []

        def start_background(self, cmd, log_path, pid_path, extra_env=None, cwd=None):
            self.calls.append(extra_env)
            pid_path.write_text("1")
            return 1

    engine = EngineConfig(
        system="sleek",
        type="llama.cpp",
        binary="/opt/llama-prism/llama-server",
        env={"LD_LIBRARY_PATH": "/opt/cuda/lib"},
    )
    system = SystemConfig(os="linux", control="local")
    runner = RecordingLocalRunner()
    adapter = LlamaCppAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path="/models/x.gguf")

    adapter.start("bonsai-27b", candidate)

    assert runner.calls == [{"LD_LIBRARY_PATH": "/opt/cuda/lib"}]


def test_llamacpp_local_start_passes_none_env_when_engine_has_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class RecordingLocalRunner(LocalRunner):
        def __init__(self):
            self.calls = []

        def start_background(self, cmd, log_path, pid_path, extra_env=None, cwd=None):
            self.calls.append(extra_env)
            pid_path.write_text("1")
            return 1

    engine = EngineConfig(system="sleek", type="llama.cpp", binary="/opt/llama/llama-server")
    system = SystemConfig(os="linux", control="local")
    runner = RecordingLocalRunner()
    adapter = LlamaCppAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path="/models/x.gguf")

    adapter.start("plain-model", candidate)

    assert runner.calls == [None]


class FakeSSHRunner(SSHRunner):
    def __init__(self):
        super().__init__("monolith")
        self.calls = []

    def run_shell(self, cmd, timeout=30):
        self.calls.append(cmd)
        if "$env:USERPROFILE" in cmd:
            return CommandResult(0, "C:\\Users\\wade", "")
        if "-Action start" in cmd:
            return CommandResult(0, json.dumps({"pid": 4242, "log_path": "C:\\Users\\wade\\.ModelManager\\logs\\x.log"}), "")
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

    start_call = next(c for c in runner.calls if "-Action start" in c)
    assert "C:\\Users\\wade\\.ModelManager\\agent.ps1" in start_call
    payload_b64 = start_call.split("-PayloadB64 ")[1]
    payload = json.loads(base64.b64decode(payload_b64))
    assert payload["args"] == ["--model", r"D:\models\x.gguf", "--port", "8081"]
    assert payload["env"] == {}
    assert payload["cwd"] is None

    # the home directory is resolved once over SSH and cached, not re-queried per call
    assert sum("$env:USERPROFILE" in c for c in runner.calls) == 1


def test_llamacpp_ssh_start_includes_engine_env():
    engine = EngineConfig(
        system="monolith",
        type="llama.cpp",
        binary=r"C:\Dev\Ollama\build\lib\ollama\llama-server.exe",
        env={
            "PATH": r"C:\Dev\Ollama\build\lib\ollama\cuda_v13",
            "GGML_BACKEND_PATH": r"C:\Dev\Ollama\build\lib\ollama\cuda_v13\ggml-cuda.dll",
        },
    )
    system = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    runner = FakeSSHRunner()
    adapter = LlamaCppAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=8090, model_path=r"C:\models\x.gguf")

    adapter.start("ennoia-27b", candidate)

    start_call = next(c for c in runner.calls if "-Action start" in c)
    payload = json.loads(base64.b64decode(start_call.split("-PayloadB64 ")[1]))
    assert payload["env"] == {
        "PATH": r"C:\Dev\Ollama\build\lib\ollama\cuda_v13",
        "GGML_BACKEND_PATH": r"C:\Dev\Ollama\build\lib\ollama\cuda_v13\ggml-cuda.dll",
    }


def test_llamacpp_log_path_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = EngineConfig(system="sleek", type="llama.cpp", binary=fake_binary(tmp_path))
    system = SystemConfig(os="linux", control="local")
    adapter = LlamaCppAdapter(engine, system, LocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path="/models/x.gguf")

    path = adapter.log_path("qwen-72b", candidate)

    assert path == str(tmp_path / "modelmanager" / "logs" / "qwen-72b.log")


def test_llamacpp_log_path_ssh():
    engine = EngineConfig(system="monolith", type="llama.cpp", binary=r"C:\engines\buildB\llama-server.exe")
    system = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    runner = FakeSSHRunner()
    adapter = LlamaCppAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path=r"D:\models\x.gguf")

    path = adapter.log_path("ennoia-27b", candidate)

    assert path == r"C:\Users\wade\.ModelManager\logs\ennoia-27b.log"


class FakeLocalRunner(LocalRunner):
    def __init__(self):
        self.shell_calls = []

    def run_shell(self, cmd, timeout=30):
        self.shell_calls.append(cmd)
        return CommandResult(1, "", "no journal")


def test_ollama_status_and_lifecycle(monkeypatch):
    engine = EngineConfig(system="sleek", type="ollama", base_url="http://localhost:11434")
    adapter = OllamaAdapter("ollama-sleek", engine, FakeLocalRunner())
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
    adapter = OllamaAdapter("ollama-sleek", engine, FakeLocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    def fake_get(url, timeout):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    status = adapter.status("nomic-embed", candidate)
    assert status.running is False


def test_ollama_self_managed_server_launches_via_agent_when_unreachable(monkeypatch):
    engine = EngineConfig(
        system="monolith",
        type="ollama",
        base_url="http://localhost:11434",
        binary=r"C:\dev\ollama\ollama.exe",
        cwd=r"C:\dev\ollama",
        env={"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": r"C:\dev\ollama\models"},
    )
    runner = FakeSSHRunner()
    adapter = OllamaAdapter("ollama-monolith", engine, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text", ollama_task="embed")

    calls = {"n": 0}

    def fake_get(url, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
        return httpx.Response(200, json={"models": []}, request=httpx.Request("GET", url))

    posted = {}

    def fake_post(url, json, timeout):
        posted["json"] = json
        return httpx.Response(200, json={"done": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    adapter.start("nomic-embed-monolith", candidate)

    start_call = next(c for c in runner.calls if "-Action start" in c)
    payload = json.loads(base64.b64decode(start_call.split("-PayloadB64 ")[1]))
    assert payload["name"] == "ollama-server-ollama-monolith"
    assert payload["binary"] == r"C:\dev\ollama\ollama.exe"
    assert payload["args"] == ["serve"]
    assert payload["cwd"] == r"C:\dev\ollama"
    assert payload["env"] == {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": r"C:\dev\ollama\models"}
    assert posted["json"]["keep_alive"] == -1


def test_ollama_self_managed_server_skips_launch_when_already_reachable(monkeypatch):
    engine = EngineConfig(
        system="monolith",
        type="ollama",
        base_url="http://localhost:11434",
        binary=r"C:\dev\ollama\ollama.exe",
    )
    runner = FakeSSHRunner()
    adapter = OllamaAdapter("ollama-monolith", engine, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text", ollama_task="embed")

    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200, json={"models": []}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: httpx.Response(200, json={"done": True}, request=httpx.Request("POST", url)))

    adapter.start("nomic-embed-monolith", candidate)

    assert not any("-Action start" in c for c in runner.calls)


def test_ollama_log_path_none_when_externally_managed():
    engine = EngineConfig(system="sleek", type="ollama", base_url="http://localhost:11434")
    adapter = OllamaAdapter("ollama-sleek", engine, FakeLocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    assert adapter.log_path("nomic-embed", candidate) is None


def test_ollama_log_path_local_when_self_managed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = EngineConfig(system="sleek", type="ollama", base_url="http://localhost:11434", binary="/opt/ollama/ollama")
    adapter = OllamaAdapter("ollama-sleek", engine, LocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    path = adapter.log_path("nomic-embed", candidate)

    assert path == str(tmp_path / "modelmanager" / "logs" / "ollama-server-ollama-sleek.log")


def test_ollama_log_path_ssh_when_self_managed():
    engine = EngineConfig(
        system="monolith", type="ollama", base_url="http://monolith:11434", binary=r"C:\dev\ollama\ollama.exe"
    )
    runner = FakeSSHRunner()
    adapter = OllamaAdapter("ollama-monolith", engine, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    path = adapter.log_path("nomic-embed-monolith", candidate)

    assert path == r"C:\Users\wade\.ModelManager\logs\ollama-server-ollama-monolith.log"
