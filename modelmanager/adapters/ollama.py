from __future__ import annotations

import time

import httpx

from .. import paths, remote_paths
from ..config import Candidate, EngineConfig, effective_port
from ..errors import EngineControlError, PortInUseError
from ..port_check import check_local_port_free
from ..runners.base import Runner
from ..runners.local import LocalRunner
from ..runners.ssh import SSHRunner
from . import windows_agent
from .base import AdapterStatus, EngineAdapter


class OllamaAdapter(EngineAdapter):
    """Controls models on an Ollama server via its own HTTP API.

    We always load with keep_alive=-1 and unload explicitly with keep_alive=0,
    since idle/swap timing is decided by our scheduler, not Ollama's own timer.

    Normally the server itself (base_url) is assumed already running -
    installed as a system/Windows service, outside our control. When
    `engine.binary` is set, though, this is a custom build that isn't
    registered as a service anywhere, so we launch and manage the 'ollama
    serve' process ourselves too (same PID/log mechanism as LlamaCppAdapter,
    including the Windows WMI launcher to survive the SSH session closing),
    starting it lazily the first time a model on this engine is started.
    """

    def __init__(self, engine_name: str, engine: EngineConfig, runner: Runner):
        self.engine_name = engine_name
        self.engine = engine
        self.base_url = engine.base_url.rstrip("/")
        self.runner = runner

    def _server_name(self) -> str:
        return f"ollama-server-{self.engine_name}"

    def _server_reachable(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/api/ps", timeout=3)
            resp.raise_for_status()
            return True
        except httpx.HTTPError:
            return False

    def _ensure_server_running(self) -> None:
        if not self.engine.binary or self._server_reachable():
            return
        name = self._server_name()
        port = effective_port(self.base_url)
        if isinstance(self.runner, LocalRunner):
            check_local_port_free(port, self.engine.system)
            self.runner.start_background(
                [self.engine.binary, "serve"],
                paths.log_file(name),
                paths.pid_file(name),
                extra_env=self.engine.env or None,
                cwd=self.engine.cwd or None,
            )
        else:
            assert isinstance(self.runner, SSHRunner)
            port_result = windows_agent.invoke(self.runner, "check-port", {"port": port})
            if not port_result.ok:
                raise PortInUseError(f"port {port} is already in use on {self.engine.system!r}: {port_result.stderr.strip() or port_result.stdout.strip()}")
            payload = {
                "name": name,
                "binary": self.engine.binary,
                "args": ["serve"],
                "env": self.engine.env,
                "cwd": self.engine.cwd,
            }
            result = windows_agent.invoke(self.runner, "start", payload)
            if not result.ok:
                raise EngineControlError(f"failed to start ollama server for engine {self.engine_name!r}: {result.stderr}")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._server_reachable():
                return
            time.sleep(0.5)
        raise EngineControlError(f"ollama server for engine {self.engine_name!r} did not become reachable within 30s")

    def _warm_endpoint(self, candidate: Candidate) -> tuple[str, dict]:
        if candidate.ollama_task == "embed":
            return "/api/embed", {"model": candidate.model_ref, "input": ""}
        return "/api/generate", {"model": candidate.model_ref, "prompt": ""}

    def start(self, definition_name: str, candidate: Candidate) -> int:
        self._ensure_server_running()
        path, body = self._warm_endpoint(candidate)
        resp = httpx.post(f"{self.base_url}{path}", json={**body, "keep_alive": -1}, timeout=120)
        self._raise_clean(resp, candidate)
        return effective_port(self.base_url)

    def stop(self, definition_name: str, candidate: Candidate) -> None:
        path, body = self._warm_endpoint(candidate)
        resp = httpx.post(f"{self.base_url}{path}", json={**body, "keep_alive": 0}, timeout=30)
        self._raise_clean(resp, candidate)

    def _raise_clean(self, resp: httpx.Response, candidate: Candidate) -> None:
        """Ollama's own error body (e.g. "model ... not found, try pulling
        it first") is far more actionable than a bare 404 - surface it as an
        EngineControlError so ensure()/verify() turn it into a clean, coded
        502 instead of an opaque 500 from an unhandled HTTPStatusError."""
        if resp.is_success:
            return
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise EngineControlError(f"ollama rejected model_ref {candidate.model_ref!r} on engine {self.engine_name!r} ({resp.status_code}): {detail}")

    def status(self, definition_name: str, candidate: Candidate) -> AdapterStatus:
        try:
            resp = httpx.get(f"{self.base_url}/api/ps", timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return AdapterStatus(running=False, detail=f"ollama unreachable: {exc}")
        for model in resp.json().get("models", []):
            if model.get("name") == candidate.model_ref or model.get("model") == candidate.model_ref:
                return AdapterStatus(running=True)
        return AdapterStatus(running=False)

    def logs(self, definition_name: str, candidate: Candidate, lines: int = 200) -> str:
        if self.engine.binary:
            # Self-managed server: no system service/journal to query, but we
            # have its own PID-file log, same as a llama.cpp process.
            name = self._server_name()
            if isinstance(self.runner, LocalRunner):
                return self.runner.tail(paths.log_file(name), lines)
            assert isinstance(self.runner, SSHRunner)
            result = windows_agent.invoke(self.runner, "logs", {"name": name, "lines": lines})
            return result.stdout if result.ok else result.stderr

        # Ollama is usually a system-wide service, but try --user too for
        # setups that run it per-user. journalctl exits 0 either way and
        # prints the "-- No entries --" placeholder instead of failing, so
        # that placeholder (not just exit code) is what tells us to move on.
        for cmd in (
            f"journalctl -u ollama -n {lines} --no-pager",
            f"journalctl --user -u ollama -n {lines} --no-pager",
        ):
            result = self.runner.run_shell(cmd, timeout=10)
            output = result.stdout.strip()
            if result.ok and output and output != "-- No entries --":
                return result.stdout
        return "ollama logs unavailable via journalctl on this system; check the ollama service directly"

    def log_paths(self, definition_name: str, candidate: Candidate) -> list[str]:
        if not self.engine.binary:
            return []  # externally-managed service; only journalctl has these logs, no discrete file
        name = self._server_name()
        if isinstance(self.runner, LocalRunner):
            return [str(paths.log_file(name))]
        assert isinstance(self.runner, SSHRunner)
        return [
            remote_paths.windows_agent_log_path(self.runner, name),
            remote_paths.windows_agent_err_log_path(self.runner, name),
        ]
