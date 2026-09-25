from __future__ import annotations

from ..config import EngineConfig, Registry, SystemConfig
from ..runners.base import Runner
from ..runners.local import LocalRunner
from ..runners.ssh import SSHRunner
from .base import EngineAdapter
from .llamacpp import LlamaCppAdapter
from .ollama import OllamaAdapter


def build_runner(system: SystemConfig) -> Runner:
    if system.control == "local":
        return LocalRunner()
    return SSHRunner(system.ssh_host)


def build_adapter(engine_name: str, registry: Registry) -> EngineAdapter:
    engine = registry.engines[engine_name]
    system = registry.systems[engine.system]
    runner = build_runner(system)
    if engine.type == "ollama":
        return OllamaAdapter(engine, runner)
    return LlamaCppAdapter(engine, system, runner)


class AdapterRegistry:
    """Caches one adapter instance per engine (each engine keeps its own runner)."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self._cache: dict[str, EngineAdapter] = {}

    def get(self, engine_name: str) -> EngineAdapter:
        if engine_name not in self._cache:
            self._cache[engine_name] = build_adapter(engine_name, self.registry)
        return self._cache[engine_name]

    def invalidate(self, engine_name: str) -> None:
        self._cache.pop(engine_name, None)

    def clear(self) -> None:
        self._cache.clear()
