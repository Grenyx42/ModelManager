from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


def effective_port(base_url: str) -> int:
    """The port a base_url actually serves on, filling in the scheme's
    default (80/443) when the URL doesn't spell one out explicitly."""
    parsed = urlparse(base_url)
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


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
    type: Literal["ollama", "llama.cpp", "process"] = Field(
        description="Which backend this engine is. Determines the control protocol: 'ollama' is controlled via its own HTTP API; 'llama.cpp' is a raw llama-server process controlled by PID/log file (locally) or the Windows PowerShell agent (remotely); 'process' is the same PID/log-based control as 'llama.cpp' but for an arbitrary executable (a custom proxy/server) instead of assuming llama-server's own --model/--port CLI convention - the candidate's 'args' must spell out the full command line."
    )
    base_url: Optional[str] = Field(
        default=None, description="Required when type='ollama'. The Ollama server's HTTP base URL, e.g. 'http://localhost:11434'."
    )
    binary: Optional[str] = Field(
        default=None,
        description=(
            "Required when type='llama.cpp' or type='process': absolute path to the executable (the "
            "llama-server binary for a specific build, or e.g. a python.exe for a 'process' engine) - you "
            "can have several engines pointing at different builds/interpreters. Optional when "
            "type='ollama': if set, the coordinator launches and manages 'ollama serve' itself using this "
            "binary path, instead of assuming a server is already running externally at base_url - use this "
            "for a custom Ollama build that isn't registered as a system/Windows service. Leave unset for a "
            "normal Ollama install where base_url is already reachable on its own."
        ),
    )
    cwd: Optional[str] = Field(
        default=None,
        description=(
            "Working directory to launch the process from, if it matters for this build (e.g. a custom "
            "Ollama build that resolves relative paths from its own install directory). Optional; omit if "
            "the binary doesn't care about its working directory."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra environment variables merged into the process's environment when this engine launches a "
            "process itself: always for llama.cpp candidates, and for ollama only when binary is set (a "
            "self-managed 'ollama serve'). Ignored for an ollama engine with no binary set (externally-managed "
            "server). Common uses: LD_LIBRARY_PATH for a build whose CUDA/cuBLAS libraries aren't on the "
            "system's default library path; OLLAMA_HOST/OLLAMA_MODELS/etc. for a custom Ollama build."
        ),
    )

    @model_validator(mode="after")
    def _fields_for_type(self) -> "EngineConfig":
        if self.type == "ollama" and not self.base_url:
            raise ValueError("ollama engines require base_url")
        if self.type in ("llama.cpp", "process") and not self.binary:
            raise ValueError(f"{self.type} engines require binary")
        return self


class HealthCheck(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    path: str = Field(description="HTTP path on the candidate's own host:port to call for `verify`, e.g. '/health' or '/v1/chat/completions'.")
    body: Optional[dict] = Field(default=None, description="JSON body for a POST check. Ignored for GET.")


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
    args: list[str] = Field(
        default_factory=list,
        description=(
            "For llama.cpp: extra CLI args appended after the auto-added --model/--port, e.g. "
            "['--ctx-size', '8192']. For process: the engine binary's *entire* command line (nothing is "
            "auto-added), e.g. ['-u', 'C:\\\\dev\\\\proxy\\\\server.py']. Ignored for ollama."
        ),
    )
    ollama_task: Literal["generate", "embed"] = Field(
        default="generate",
        description="Only meaningful for ollama candidates. Ollama has no single 'load this model' call: text/chat models warm up via /api/generate, embedding models via /api/embed, and using the wrong one 400s. Set 'embed' for embedding models.",
    )
    health_check: Optional[HealthCheck] = Field(
        default=None,
        description="Overrides what `verify` calls to confirm this candidate is actually answering requests. Required for a 'process' engine (which has no assumed API shape) if you want it to be verifiable at all; optional for ollama/llama.cpp, where it overrides their built-in default check.",
    )


class ModelDefinition(BaseModel):
    pinned: bool = Field(
        default=False,
        description="If true, this model is never auto-evicted by the idle sweep or by another model's ensure() call, and the coordinator starts it automatically on startup. Use for standing dependencies like an embedding model other apps assume is always up.",
    )
    idle_timeout_seconds: Optional[int] = Field(
        default=None,
        description=(
            "Seconds of no ensure() calls before the idle sweep proactively stops this model. "
            "Also gates how long it must sit idle before another model's ensure() can reactively "
            "swap it out to free its gpu_group. Leaving this null does NOT mean 'never evict' - it "
            "means 'never evict on a timer': the idle sweep leaves it alone indefinitely, but it's "
            "still evicted the instant another model actually needs its gpu_group (immediately, no "
            "idle grace period - only an active lease blocks that). Use pinned=true instead for a "
            "model that must truly never be evicted by anything. Ignored (and irrelevant) when "
            "pinned=true."
        ),
    )
    candidates: list[Candidate] = Field(
        description="Ordered list of backends that can serve this logical model (e.g. the same model available via a local llama.cpp build and a build on another machine). ensure() tries them in order, preferring one already loaded, else the first whose gpu_group is free or freeable. At least one required."
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "Other model names (keys in the top-level 'models' map) that must be ensured before this "
            "one. ensure()/acquire() recursively ensures every dependency first, in order; a "
            "dependency's failure fails this model's ensure() too. No cycles allowed (validated across "
            "the whole registry, not just within one model)."
        ),
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
    groups: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Named sets of existing model names (often spanning several systems/engines) that belong "
            "to one workflow, so they can be ensured/verified/stopped together as a unit via "
            "/v1/groups/{name}/ensure|verify|stop - best-effort, one call per member model, no new "
            "gpu-exclusivity semantics. Purely a reference list: each member must already be a key in "
            "'models', and still has exactly one definition there (no duplication)."
        ),
    )

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
                if engine.type == "ollama":
                    expected_port = effective_port(engine.base_url)
                    if candidate.port != expected_port:
                        raise ValueError(
                            f"model {model_name!r} candidate port {candidate.port} doesn't match engine "
                            f"{candidate.engine!r}'s base_url port {expected_port} - ollama candidates must "
                            "use the engine's actual port"
                        )
        for group_name, member_names in self.groups.items():
            for member in member_names:
                if member not in self.models:
                    raise ValueError(f"group {group_name!r} references unknown model {member!r}")

        for model_name, model in self.models.items():
            for dep in model.depends_on:
                if dep not in self.models:
                    raise ValueError(f"model {model_name!r} depends_on unknown model {dep!r}")
                if dep == model_name:
                    raise ValueError(f"model {model_name!r} cannot depend on itself")

        # Cycle check across the whole depends_on graph, so ensure()'s own
        # recursion never has to guard against one - a cycle is rejected here,
        # at registry-write time, before it can ever reach runtime.
        visiting: set[str] = set()
        visited: set[str] = set()

        def _visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"depends_on cycle detected involving model {name!r}")
            visiting.add(name)
            for dep in self.models[name].depends_on:
                _visit(dep)
            visiting.discard(name)
            visited.add(name)

        for model_name in self.models:
            _visit(model_name)

        # Ollama ports are static and structural (the engine's base_url, shared
        # by every candidate that uses it) - nothing dynamically remaps one, so
        # two *different* ollama engines claiming the same (system, port) is
        # always a real misconfiguration. llama.cpp/process candidates each get
        # their own process per start() and their port is resolved live (see
        # port_check.resolve_port) - a declared collision there isn't a
        # registry-time concern.
        ollama_port_claims: dict[tuple[str, int], set[str]] = {}
        for model_name, model in self.models.items():
            for candidate in model.candidates:
                engine = self.engines[candidate.engine]
                if engine.type != "ollama":
                    continue
                ollama_port_claims.setdefault((engine.system, candidate.port), set()).add(candidate.engine)
        for (system_name, port), engine_names in ollama_port_claims.items():
            if len(engine_names) > 1:
                raise ValueError(
                    f"port {port} on system {system_name!r} is claimed by more than one ollama engine: "
                    f"{sorted(engine_names)} - each needs its own port"
                )
        return self


def default_registry_path() -> Path:
    env_path = os.environ.get("MODELMANAGER_REGISTRY")
    if env_path:
        return Path(env_path)
    # Not repo-relative: an Omarchy-plugin-marketplace install lives wherever
    # `omarchy plugin add` cloned it (e.g. ~/.config/omarchy/plugins/<id>/),
    # and that directory can be replaced wholesale on an update - a fixed
    # XDG config location survives that regardless of where the code lives.
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg_config) / "modelmanager" / "registry.json"


def default_token_path() -> Path:
    env_path = os.environ.get("MODELMANAGER_TOKEN_PATH")
    if env_path:
        return Path(env_path)
    # Same reasoning/XDG convention as default_registry_path() above - a
    # fixed location independent of wherever the coordinator binary lives.
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg_config) / "modelmanager" / "token"


def default_tls_cert_path() -> Path:
    env_path = os.environ.get("MODELMANAGER_TLS_CERT_PATH")
    if env_path:
        return Path(env_path)
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg_config) / "modelmanager" / "tls" / "cert.pem"


def default_tls_key_path() -> Path:
    env_path = os.environ.get("MODELMANAGER_TLS_KEY_PATH")
    if env_path:
        return Path(env_path)
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg_config) / "modelmanager" / "tls" / "key.pem"


def default_backup_path(registry_path: Path) -> Path:
    """Where the last known-good registry gets snapshotted (see
    Manager.save_backup) - read back by load_registry_or_empty() at startup
    if the live file fails to load, so a bad hand-edit doesn't leave the
    coordinator with nothing instead of a slightly-stale but working config."""
    return registry_path.with_suffix(registry_path.suffix + ".last-good")


def load_registry(path: Optional[Path] = None) -> Registry:
    path = path or default_registry_path()
    if not path.exists():
        raise FileNotFoundError(
            f"registry file not found at {path}; copy registry.example.json to registry.json and edit it"
        )
    data = json.loads(path.read_text())
    return Registry.model_validate(data)
