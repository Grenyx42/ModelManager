import base64
import json
import textwrap

import httpx
import pytest

import socket

from modelmanager.adapters.llamacpp import LlamaCppAdapter
from modelmanager.adapters.ollama import OllamaAdapter
from modelmanager.adapters.process import ProcessAdapter
from modelmanager.config import Candidate, EngineConfig, SystemConfig
from modelmanager.errors import EngineControlError, PortInUseError
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


def test_llamacpp_local_start_raises_when_process_crashes_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # Needs a real (if tiny) grace period here specifically - unlike every
    # other test in this file, this one relies on the child process actually
    # having exited by the time start() checks is_running(), which a
    # patched-to-0 sleep can't guarantee.
    monkeypatch.setattr("modelmanager.adapters.llamacpp.STARTUP_GRACE_SECONDS", 0.2)
    crashing_binary = tmp_path / "fake-llama-server-crash"
    crashing_binary.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "error: could not find model file" >&2
        exit 1
    """))
    crashing_binary.chmod(0o755)
    engine = EngineConfig(system="sleek", type="llama.cpp", binary=str(crashing_binary))
    system = SystemConfig(os="linux", control="local")
    adapter = LlamaCppAdapter(engine, system, LocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path="/models/missing.gguf")

    with pytest.raises(EngineControlError, match="could not find model file"):
        adapter.start("bad-model", candidate)

    # the pid file must not be left behind claiming a dead process is active
    from modelmanager import paths
    assert not paths.pid_file("bad-model").exists()


def test_llamacpp_local_start_resolves_a_different_port_when_preferred_is_taken(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = EngineConfig(system="sleek", type="llama.cpp", binary=fake_binary(tmp_path))
    system = SystemConfig(os="linux", control="local")
    adapter = LlamaCppAdapter(engine, system, LocalRunner())

    occupying_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupying_socket.bind(("0.0.0.0", 0))
    occupying_socket.listen(1)
    port = occupying_socket.getsockname()[1]
    try:
        candidate = Candidate(engine="e", gpu_group="g", port=port, model_path="/models/x.gguf")
        actual_port = adapter.start("qwen-72b", candidate)

        assert actual_port != port  # resolved to a free one instead of the taken preferred port
        status = adapter.status("qwen-72b", candidate)
        assert status.running is True
        assert status.port == actual_port

        from modelmanager import paths
        assert paths.port_file("qwen-72b").read_text().strip() == str(actual_port)
    finally:
        occupying_socket.close()
        adapter.stop("qwen-72b", candidate)


def test_llamacpp_local_start_passes_engine_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class RecordingLocalRunner(LocalRunner):
        def __init__(self):
            self.calls = []

        def start_background(self, cmd, log_path, pid_path, extra_env=None, cwd=None):
            self.calls.append(extra_env)
            pid_path.write_text("1")
            return 1

        def is_running(self, pid):
            return True

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

        def is_running(self, pid):
            return True

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
        if "-Action check-port" in cmd:
            return CommandResult(0, json.dumps({"free": True}), "")
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


def test_llamacpp_ssh_start_resolves_a_different_port_when_preferred_is_taken():
    engine = EngineConfig(system="monolith", type="llama.cpp", binary=r"C:\engines\buildB\llama-server.exe")
    system = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    runner = FakeSSHRunner()

    def fake_run_shell(cmd, timeout=30):
        if "-Action check-port" in cmd:
            runner.calls.append(cmd)
            payload = json.loads(base64.b64decode(cmd.split("-PayloadB64 ")[1]))
            if payload["port"] == 8081:
                return CommandResult(1, "", "port 8081 is already in use")
            return CommandResult(0, "", "")
        return FakeSSHRunner.run_shell(runner, cmd, timeout=timeout)

    runner.run_shell = fake_run_shell
    adapter = LlamaCppAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path=r"D:\models\x.gguf")

    actual_port = adapter.start("qwen-72b", candidate)

    assert actual_port == 8082
    start_call = next(c for c in runner.calls if "-Action start" in c)
    payload = json.loads(base64.b64decode(start_call.split("-PayloadB64 ")[1]))
    assert payload["port"] == 8082
    assert payload["args"] == ["--model", r"D:\models\x.gguf", "--port", "8082"]


def test_llamacpp_log_paths_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = EngineConfig(system="sleek", type="llama.cpp", binary=fake_binary(tmp_path))
    system = SystemConfig(os="linux", control="local")
    adapter = LlamaCppAdapter(engine, system, LocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path="/models/x.gguf")

    paths_ = adapter.log_paths("qwen-72b", candidate)

    assert paths_ == [str(tmp_path / "modelmanager" / "logs" / "qwen-72b.log")]


def test_llamacpp_log_paths_ssh_includes_stdout_and_stderr():
    engine = EngineConfig(system="monolith", type="llama.cpp", binary=r"C:\engines\buildB\llama-server.exe")
    system = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    runner = FakeSSHRunner()
    adapter = LlamaCppAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=8081, model_path=r"D:\models\x.gguf")

    paths_ = adapter.log_paths("ennoia-27b", candidate)

    # llama-server logs almost entirely to stderr - both files must be
    # returned, or a caller tailing only the first would see next to nothing.
    assert paths_ == [
        r"C:\Users\wade\.ModelManager\logs\ennoia-27b.log",
        r"C:\Users\wade\.ModelManager\logs\ennoia-27b.err.log",
    ]


def test_process_local_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = EngineConfig(system="coder", type="process", binary=fake_binary(tmp_path))
    system = SystemConfig(os="linux", control="local")
    adapter = ProcessAdapter(engine, system, LocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=5000, args=["-u", "server.py"])

    adapter.start("proxy", candidate)
    status = adapter.status("proxy", candidate)
    assert status.running is True
    assert status.pid is not None

    adapter.stop("proxy", candidate)
    status = adapter.status("proxy", candidate)
    assert status.running is False


def test_process_ssh_start_passes_args_through_unmodified():
    # The whole point of 'process' vs 'llama.cpp': no --model/--port is
    # auto-added, since an arbitrary proxy/server has its own CLI convention.
    engine = EngineConfig(
        system="coder",
        type="process",
        binary=r"C:\users\wade\.pyenv\pyenv-win\versions\3.10.11\python.exe",
        env={"PYTHONIOENCODING": "utf-8"},
    )
    system = SystemConfig(os="windows", control="ssh", ssh_host="coder")
    runner = FakeSSHRunner()
    adapter = ProcessAdapter(engine, system, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=5000, args=["-u", r"C:\dev\freetoken\scripts\kv_proxy.py"])

    adapter.start("freetoken-proxy", candidate)
    status = adapter.status("freetoken-proxy", candidate)
    assert status.running is True

    start_call = next(c for c in runner.calls if "-Action start" in c)
    payload = json.loads(base64.b64decode(start_call.split("-PayloadB64 ")[1]))
    assert payload["args"] == ["-u", r"C:\dev\freetoken\scripts\kv_proxy.py"]
    assert payload["env"] == {"PYTHONIOENCODING": "utf-8"}


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


def test_ollama_start_raises_clean_error_on_model_not_found(monkeypatch):
    engine = EngineConfig(system="monolith", type="ollama", base_url="http://monolith:11434")
    adapter = OllamaAdapter("ollama-monolith", engine, FakeLocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="qwen2.5:7b-instruct")

    def fake_post(url, json, timeout):
        return httpx.Response(
            404, json={"error": "model \"qwen2.5:7b-instruct\" not found, try pulling it first"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(EngineControlError, match="try pulling it first"):
        adapter.start("qwen-7b-monolith", candidate)


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


def test_ollama_log_paths_empty_when_externally_managed():
    engine = EngineConfig(system="sleek", type="ollama", base_url="http://localhost:11434")
    adapter = OllamaAdapter("ollama-sleek", engine, FakeLocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    assert adapter.log_paths("nomic-embed", candidate) == []


def test_ollama_log_paths_local_when_self_managed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    engine = EngineConfig(system="sleek", type="ollama", base_url="http://localhost:11434", binary="/opt/ollama/ollama")
    adapter = OllamaAdapter("ollama-sleek", engine, LocalRunner())
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    paths_ = adapter.log_paths("nomic-embed", candidate)

    assert paths_ == [str(tmp_path / "modelmanager" / "logs" / "ollama-server-ollama-sleek.log")]


def test_ollama_log_paths_ssh_when_self_managed():
    engine = EngineConfig(
        system="monolith", type="ollama", base_url="http://monolith:11434", binary=r"C:\dev\ollama\ollama.exe"
    )
    runner = FakeSSHRunner()
    adapter = OllamaAdapter("ollama-monolith", engine, runner)
    candidate = Candidate(engine="e", gpu_group="g", port=11434, model_ref="nomic-embed-text")

    paths_ = adapter.log_paths("nomic-embed-monolith", candidate)

    assert paths_ == [
        r"C:\Users\wade\.ModelManager\logs\ollama-server-ollama-monolith.log",
        r"C:\Users\wade\.ModelManager\logs\ollama-server-ollama-monolith.err.log",
    ]
