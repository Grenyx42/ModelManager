from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class SystemConfig(BaseModel):
    os: Literal["linux", "windows"]
    control: Literal["local", "ssh"]
    ssh_host: Optional[str] = None
    # Hostname apps should use to reach engines on this system (e.g. its Tailscale
    # MagicDNS name), if different from ssh_host. Defaults to ssh_host, then localhost.
    advertise_host: Optional[str] = None

    @model_validator(mode="after")
    def _ssh_host_required(self) -> "SystemConfig":
        if self.control == "ssh" and not self.ssh_host:
            raise ValueError("ssh_host is required when control == 'ssh'")
        return self

    def resolved_host(self) -> str:
        return self.advertise_host or self.ssh_host or "localhost"


class GpuGroupConfig(BaseModel):
    system: str


class EngineConfig(BaseModel):
    system: str
    type: Literal["ollama", "llama.cpp"]
    base_url: Optional[str] = None
    binary: Optional[str] = None

    @model_validator(mode="after")
    def _fields_for_type(self) -> "EngineConfig":
        if self.type == "ollama" and not self.base_url:
            raise ValueError("ollama engines require base_url")
        if self.type == "llama.cpp" and not self.binary:
            raise ValueError("llama.cpp engines require binary")
        return self


class Candidate(BaseModel):
    engine: str
    gpu_group: str
    port: int
    model_ref: Optional[str] = None
    model_path: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    # Ollama has no single "load this model" call: generation models warm up
    # via /api/generate (prompt=""), embedding models via /api/embed
    # (input=""), and using the wrong one 400s. Ignored for llama.cpp.
    ollama_task: Literal["generate", "embed"] = "generate"


class ModelDefinition(BaseModel):
    pinned: bool = False
    idle_timeout_seconds: Optional[int] = None
    candidates: list[Candidate]

    @model_validator(mode="after")
    def _at_least_one_candidate(self) -> "ModelDefinition":
        if not self.candidates:
            raise ValueError("model definition must have at least one candidate")
        return self


class Registry(BaseModel):
    systems: dict[str, SystemConfig]
    gpu_groups: dict[str, GpuGroupConfig]
    engines: dict[str, EngineConfig]
    models: dict[str, ModelDefinition]

    @model_validator(mode="after")
    def _cross_references(self) -> "Registry":
        for group_name, group in self.gpu_groups.items():
            if group.system not in self.systems:
                raise ValueError(f"gpu_group {group_name!r} references unknown system {group.system!r}")
        for engine_name, engine in self.engines.items():
            if engine.system not in self.systems:
                raise ValueError(f"engine {engine_name!r} references unknown system {engine.system!r}")
        for model_name, model in self.models.items():
            for candidate in model.candidates:
                if candidate.engine not in self.engines:
                    raise ValueError(
                        f"model {model_name!r} references unknown engine {candidate.engine!r}"
                    )
                if candidate.gpu_group not in self.gpu_groups:
                    raise ValueError(
                        f"model {model_name!r} references unknown gpu_group {candidate.gpu_group!r}"
                    )
                engine = self.engines[candidate.engine]
                if engine.type == "ollama" and not candidate.model_ref:
                    raise ValueError(
                        f"model {model_name!r} candidate on ollama engine {candidate.engine!r} needs model_ref"
                    )
                if engine.type == "llama.cpp" and not candidate.model_path:
                    raise ValueError(
                        f"model {model_name!r} candidate on llama.cpp engine {candidate.engine!r} needs model_path"
                    )
        return self


def default_registry_path() -> Path:
    env_path = os.environ.get("MODELMANAGER_REGISTRY")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parent.parent / "registry.json"


def load_registry(path: Optional[Path] = None) -> Registry:
    path = path or default_registry_path()
    if not path.exists():
        raise FileNotFoundError(
            f"registry file not found at {path}; copy registry.example.json to registry.json and edit it"
        )
    data = json.loads(path.read_text())
    return Registry.model_validate(data)
