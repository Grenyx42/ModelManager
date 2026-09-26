from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class SystemConfig(BaseModel):
    os: Literal["linux", "windows"] = Field(description="OS running on this machine. Determines how engines are controlled remotely (SSH+shell for linux, SSH+PowerShell for windows).")
    control: Literal["local", "ssh"] = Field(
        description="'local' if this is the machine the coordinator itself runs on (engines controlled via direct subprocess/HTTP calls); 'ssh' for any other machine (controlled by shelling out over SSH)."
    )
    ssh_host: Optional[str] = Field(
        default=None,
        description="SSH alias/hostname for this machine, required when control='ssh'. Must already work non-interactively (key auth, no passphrase prompt) - typically an entry in ~/.ssh/config.",
    )
    advertise_host: Optional[str] = Field(
        default=None,
        description="Hostname callers should use to reach engines on this system (e.g. its Tailscale MagicDNS name), if different from ssh_host. Defaults to ssh_host, then 'localhost'.",
    )

    @model_validator(mode="after")
    def _ssh_host_required(self) -> "SystemConfig":
        if self.control == "ssh" and not self.ssh_host:
            raise ValueError("ssh_host is required when control == 'ssh'")
        return self

    def resolved_host(self) -> str:
        return self.advertise_host or self.ssh_host or "localhost"


class GpuGroupConfig(BaseModel):
    system: str = Field(
        description="Name of the system (key in the top-level 'systems' map) this GPU physically lives on."
    )


class EngineConfig(BaseModel):
    system: str = Field(description="Name of the system (key in the top-level 'systems' map) this engine runs on.")
    type: Literal["ollama", "llama.cpp"] = Field(
        description="Which backend this engine is. Determines the control protocol: 'ollama' is controlled via its own HTTP API; 'llama.cpp' is a raw llama-server process controlled by PID/log file (locally) or the Windows PowerShell agent (remotely)."
    )
    base_url: Optional[str] = Field(
        default=None, description="Required when type='ollama'. The Ollama server's HTTP base URL, e.g. 'http://localhost:11434'."
    )
    binary: Optional[str] = Field(
        default=None,
        description="Required when type='llama.cpp'. Absolute path to the llama-server binary for this specific build (you can have several engines pointing at different custom builds).",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra environment variables merged into the process's environment when starting a local "
            "llama.cpp candidate on this engine (ignored for ollama and for remote/ssh systems). Common "
            "use: LD_LIBRARY_PATH, when the build's CUDA/cuBLAS libraries aren't on the system's default "
            "library path (e.g. installed via a pip nvidia-* package inside some venv rather than system-wide)."
        ),
    )

    @model_validator(mode="after")
    def _fields_for_type(self) -> "EngineConfig":
        if self.type == "ollama" and not self.base_url:
            raise ValueError("ollama engines require base_url")
        if self.type == "llama.cpp" and not self.binary:
            raise ValueError("llama.cpp engines require binary")
        return self


class Candidate(BaseModel):
    engine: str = Field(description="Name of an entry in the top-level 'engines' map that can serve this model.")
    gpu_group: str = Field(
        description="Name of an entry in the top-level 'gpu_groups' map. Only one model may be active per gpu_group at a time; ensure() will swap out whatever else occupies this group once it goes idle."
    )
    port: int = Field(description="Port this candidate listens/will listen on once loaded. Returned to callers by ensure().")
    model_ref: Optional[str] = Field(
        default=None, description="Required when the engine's type is 'ollama'. The model name/tag Ollama knows it by, e.g. 'qwen2.5:7b-instruct'."
    )
    model_path: Optional[str] = Field(
        default=None,
        description="Required when the engine's type is 'llama.cpp'. Absolute path to the model file (.gguf) on the engine's own system - use the target system's native path style (e.g. 'D:\\\\models\\\\x.gguf' on Windows).",
    )
    args: list[str] = Field(default_factory=list, description="Extra CLI args appended when launching a llama.cpp candidate, e.g. ['--ctx-size', '8192']. Ignored for ollama.")
    ollama_task: Literal["generate", "embed"] = Field(
        default="generate",
        description="Only meaningful for ollama candidates. Ollama has no single 'load this model' call: text/chat models warm up via /api/generate, embedding models via /api/embed, and using the wrong one 400s. Set 'embed' for embedding models.",
    )


class ModelDefinition(BaseModel):
    pinned: bool = Field(
        default=False,
        description="If true, this model is never auto-evicted by the idle sweep or by another model's ensure() call, and the coordinator starts it automatically on startup. Use for standing dependencies like an embedding model other apps assume is always up.",
    )
    idle_timeout_seconds: Optional[int] = Field(
        default=None,
        description="Seconds of no ensure() calls before the idle sweep stops this model. Also the threshold used to decide whether this model can be swapped out to free its gpu_group for another request. Ignored (and irrelevant) when pinned=true.",
    )
    candidates: list[Candidate] = Field(
        description="Ordered list of backends that can serve this logical model (e.g. the same model available via a local llama.cpp build and a build on another machine). ensure() tries them in order, preferring one already loaded, else the first whose gpu_group is free or freeable. At least one required."
    )

    @model_validator(mode="after")
    def _at_least_one_candidate(self) -> "ModelDefinition":
        if not self.candidates:
            raise ValueError("model definition must have at least one candidate")
        return self


class Registry(BaseModel):
    systems: dict[str, SystemConfig] = Field(description="Every machine the coordinator knows about, keyed by a short name you choose (e.g. 'sleek').")
    gpu_groups: dict[str, GpuGroupConfig] = Field(description="Every exclusive-use GPU slot, keyed by a short name you choose (e.g. 'sleek-gpu0'). One active model per group at a time.")
    engines: dict[str, EngineConfig] = Field(description="Every engine instance (an Ollama server, or one llama.cpp build) that can host models, keyed by a short name you choose.")
    models: dict[str, ModelDefinition] = Field(description="Every logical model definition callers ask for by name via /v1/models/{name}/ensure.")

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
