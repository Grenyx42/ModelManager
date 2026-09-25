from __future__ import annotations

import httpx

from ..config import Candidate, EngineConfig
from ..runners.base import Runner
from .base import AdapterStatus, EngineAdapter


class OllamaAdapter(EngineAdapter):
    """Controls models on an Ollama server via its own HTTP API.

    We always load with keep_alive=-1 and unload explicitly with keep_alive=0,
    since idle/swap timing is decided by our scheduler, not Ollama's own timer.
    """

    def __init__(self, engine: EngineConfig, runner: Runner):
        self.base_url = engine.base_url.rstrip("/")
        self.runner = runner

    def _warm_endpoint(self, candidate: Candidate) -> tuple[str, dict]:
        if candidate.ollama_task == "embed":
            return "/api/embed", {"model": candidate.model_ref, "input": ""}
        return "/api/generate", {"model": candidate.model_ref, "prompt": ""}

    def start(self, definition_name: str, candidate: Candidate) -> None:
        path, body = self._warm_endpoint(candidate)
        resp = httpx.post(f"{self.base_url}{path}", json={**body, "keep_alive": -1}, timeout=120)
        resp.raise_for_status()

    def stop(self, definition_name: str, candidate: Candidate) -> None:
        path, body = self._warm_endpoint(candidate)
        resp = httpx.post(f"{self.base_url}{path}", json={**body, "keep_alive": 0}, timeout=30)
        resp.raise_for_status()

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
