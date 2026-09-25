from __future__ import annotations

import typing

from .config import Candidate, EngineConfig, GpuGroupConfig, ModelDefinition, SystemConfig


def _describe_type(annotation) -> dict:
    origin = typing.get_origin(annotation)

    if origin is typing.Literal:
        return {"type": "enum", "values": list(typing.get_args(annotation))}

    if origin in (list, tuple):
        args = typing.get_args(annotation)
        inner = _describe_type(args[0]) if args else {"type": "any"}
        return {"type": "list", "items": inner}

    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            inner = _describe_type(args[0])
            inner["nullable"] = True
            return inner
        return {"type": "union", "options": [_describe_type(a) for a in args]}

    if annotation is Candidate:
        return {"type": "object", "schema": "candidate"}

    return {"type": getattr(annotation, "__name__", str(annotation))}


def describe_fields(model_cls: type) -> dict:
    fields = {}
    for name, info in model_cls.model_fields.items():
        entry = _describe_type(info.annotation)
        entry["required"] = info.is_required()
        if not info.is_required() and info.default is not None:
            entry["default"] = info.default
        if info.description:
            entry["description"] = info.description
        fields[name] = entry
    return fields


def build_help() -> dict:
    return {
        "overview": (
            "ModelManager coordinates LLM engines (Ollama, llama.cpp) across several machines. "
            "It is configured by four kinds of named entries - systems, engines, gpu_groups, and "
            "models - that reference each other by name. Create them in that order (a model's "
            "candidates reference engines and gpu_groups that must already exist), then call "
            "POST /v1/models/{name}/ensure to actually load a model and get back a host:port."
        ),
        "concepts": {
            "system": "A machine the coordinator can control: itself (control='local') or a remote box over SSH (control='ssh').",
            "engine": "One running-or-startable backend instance on a system: an Ollama server, or one specific llama.cpp build.",
            "gpu_group": "One exclusive-use GPU slot on a system. Only one model may be active per gpu_group; requesting a second swaps out the first once it's idle.",
            "model": "A logical model name callers ask for. Has one or more 'candidates' (engine+gpu_group+path combos) that can serve it, tried in order.",
        },
        "schema": {
            "system": {
                "fields": describe_fields(SystemConfig),
                "example": {"os": "windows", "control": "ssh", "ssh_host": "monolith"},
                "create_endpoint": "POST /v1/systems/{name}",
            },
            "gpu_group": {
                "fields": describe_fields(GpuGroupConfig),
                "example": {"system": "monolith"},
                "create_endpoint": "POST /v1/gpu_groups/{name}",
            },
            "engine": {
                "fields": describe_fields(EngineConfig),
                "example": {"system": "monolith", "type": "llama.cpp", "binary": "C:\\engines\\buildB\\llama-server.exe"},
                "create_endpoint": "POST /v1/engines/{name}",
            },
            "candidate": {
                "fields": describe_fields(Candidate),
                "example": {
                    "engine": "llamacpp-monolith-b",
                    "gpu_group": "monolith-gpu0",
                    "port": 8081,
                    "model_path": "D:\\models\\qwen2.5-72b-q4.gguf",
                    "args": ["--ctx-size", "8192"],
                },
                "note": "Not created standalone - it's an entry in a model definition's 'candidates' list.",
            },
            "model": {
                "fields": describe_fields(ModelDefinition),
                "example": {
                    "pinned": False,
                    "idle_timeout_seconds": 900,
                    "candidates": [
                        {
                            "engine": "llamacpp-buildA",
                            "gpu_group": "sleek-gpu0",
                            "port": 8081,
                            "model_path": "/models/qwen2.5-72b-q4.gguf",
                            "args": ["--ctx-size", "8192"],
                        }
                    ],
                },
                "create_endpoint": "POST /v1/models/{name}",
            },
        },
        "endpoints": [
            {"method": "GET", "path": "/v1/help", "description": "this document"},
            {"method": "GET", "path": "/v1/systems", "description": "list all systems"},
            {"method": "GET", "path": "/v1/systems/{name}", "description": "get one system"},
            {"method": "POST", "path": "/v1/systems/{name}", "description": "create or update a system. Body: the system schema above.", "body_schema": "system"},
            {"method": "GET", "path": "/v1/systems/{name}/stats", "description": "GPU/VRAM stats for a system (nvidia-smi, local or over SSH)"},
            {"method": "GET", "path": "/v1/gpu_groups", "description": "list all GPU groups"},
            {"method": "GET", "path": "/v1/gpu_groups/{name}", "description": "get one GPU group"},
            {"method": "POST", "path": "/v1/gpu_groups/{name}", "description": "create or update a GPU group. Body: the gpu_group schema above.", "body_schema": "gpu_group"},
            {"method": "GET", "path": "/v1/engines", "description": "list all engines"},
            {"method": "GET", "path": "/v1/engines/{name}", "description": "get one engine"},
            {"method": "POST", "path": "/v1/engines/{name}", "description": "create or update an engine. Body: the engine schema above.", "body_schema": "engine"},
            {"method": "GET", "path": "/v1/models", "description": "list all model definitions with live status"},
            {"method": "GET", "path": "/v1/models/{name}", "description": "get one model definition's live status"},
            {"method": "POST", "path": "/v1/models/{name}", "description": "create or update a model definition. Body: the model schema above. Every candidate's engine and gpu_group must already exist.", "body_schema": "model"},
            {"method": "POST", "path": "/v1/models/{name}/ensure", "description": "load this model (starting/swapping as needed) and return {host, port}"},
            {"method": "POST", "path": "/v1/models/{name}/stop", "description": "stop this model if running"},
            {"method": "GET", "path": "/v1/models/{name}/logs", "description": "recent logs for this model (?lines=200)"},
            {"method": "GET", "path": "/v1/status/summary", "description": "compact running-model summary, for dashboards/bar widgets"},
        ],
        "notes": [
            "All writes (POST) validate the full registry (including cross-references between systems/engines/gpu_groups/models) before accepting the change, and persist to registry.json on success. An invalid write returns 422 with a specific error and changes nothing.",
            "There is currently no delete endpoint; remove an entry by editing registry.json directly and restarting.",
        ],
    }
