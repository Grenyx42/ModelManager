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
            "POST /v1/models/{name}/ensure (or /acquire) to actually load a model and get back "
            "a host:port."
        ),
        "concepts": {
            "system": "A machine the coordinator can control: itself (control='local') or a remote box over SSH (control='ssh').",
            "engine": "One running-or-startable backend instance on a system: an Ollama server, or one specific llama.cpp build.",
            "gpu_group": "One exclusive-use GPU slot on a system. Only one model may be active per gpu_group; requesting a second swaps out the first once it's idle or its lease is released.",
            "model": "A logical model name callers ask for. Has one or more 'candidates' (engine+gpu_group+path combos) that can serve it, tried in order.",
            "lease": (
                "A time-boxed claim (from /acquire) that a model is actively in use right now. "
                "While any lease is held, that model can never be auto-evicted by the idle sweep "
                "or swapped out by another model's ensure()/acquire() call, no matter what "
                "idle_timeout_seconds says. Leases expire on their own (default 300s) if never "
                "released, so a crashed caller can't permanently pin a GPU slot. Use ensure() "
                "instead of acquire() for fire-and-forget prewarming where you don't need that "
                "protection."
            ),
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
            {
                "method": "GET",
                "path": "/v1/access-log",
                "description": "recent incoming REST requests (client, method, path, status, duration), for any caller with no shell on this box. ?lines=200 to control how many.",
            },
            {"method": "GET", "path": "/v1/config", "description": "full current registry (systems, engines, gpu_groups, models) as one document"},
            {
                "method": "POST",
                "path": "/v1/config/reload",
                "description": (
                    "re-read registry.json from disk and apply it live, no restart needed. "
                    "Use this after hand-editing the file (e.g. to delete an entry - there is no "
                    "delete endpoint). Stops any active model whose definition was actually removed, "
                    "drops cached adapters so changed engine/system config takes effect, and "
                    "auto-starts any newly-pinned model. A renamed (not removed) active model - same "
                    "candidates, new key - keeps running under its new name instead of being stopped; "
                    "see the 'renamed' field in the response. Returns what changed."
                ),
            },
            {"method": "GET", "path": "/v1/systems", "description": "list all systems"},
            {"method": "GET", "path": "/v1/systems/{name}", "description": "get one system"},
            {"method": "POST", "path": "/v1/systems/{name}", "description": "create or update a system. Body: the system schema above.", "body_schema": "system"},
            {"method": "GET", "path": "/v1/systems/{name}/stats", "description": "GPU/VRAM stats for a system (nvidia-smi, local or over SSH)"},
            {
                "method": "POST",
                "path": "/v1/systems/{name}/deploy-agent",
                "description": (
                    "for windows/ssh systems only: upload scripts/windows/agent.ps1 to C:\\ModelManager\\agent.ps1 "
                    "on this system over scp, but only if its content differs from what's already there. "
                    "?force=true skips the hash check and re-uploads unconditionally. No-op (action: 'skipped') "
                    "for local or non-windows systems."
                ),
            },
            {
                "method": "POST",
                "path": "/v1/deploy-agent",
                "description": "same as above, applied to every windows/ssh system in the registry. Also runs automatically (best-effort, non-blocking) whenever the coordinator starts.",
            },
            {"method": "GET", "path": "/v1/gpu_groups", "description": "list all GPU groups"},
            {"method": "GET", "path": "/v1/gpu_groups/{name}", "description": "get one GPU group"},
            {"method": "POST", "path": "/v1/gpu_groups/{name}", "description": "create or update a GPU group. Body: the gpu_group schema above.", "body_schema": "gpu_group"},
            {"method": "GET", "path": "/v1/engines", "description": "list all engines"},
            {"method": "GET", "path": "/v1/engines/{name}", "description": "get one engine"},
            {"method": "POST", "path": "/v1/engines/{name}", "description": "create or update an engine. Body: the engine schema above.", "body_schema": "engine"},
            {"method": "GET", "path": "/v1/models", "description": "list all model definitions with live status"},
            {"method": "GET", "path": "/v1/models/{name}", "description": "get one model definition's live status"},
            {"method": "POST", "path": "/v1/models/{name}", "description": "create or update a model definition. Body: the model schema above. Every candidate's engine and gpu_group must already exist.", "body_schema": "model"},
            {"method": "POST", "path": "/v1/models/{name}/ensure", "description": "load this model (starting/swapping as needed) and return {host, port}. Does not take a lease - a concurrent idle sweep or another model's request can still evict it once idle_timeout_seconds passes."},
            {
                "method": "POST",
                "path": "/v1/models/{name}/acquire",
                "description": (
                    "like ensure(), but also takes a usage lease so this model can't be evicted or "
                    "swapped out while you hold it, regardless of idle_timeout_seconds. Returns "
                    "{host, port, lease_id, expires_in}. Optional query param ttl_seconds (default 300) "
                    "sets how long the lease lasts if never released - a safety net if your process crashes."
                ),
            },
            {
                "method": "POST",
                "path": "/v1/models/{name}/release",
                "description": (
                    "end a lease taken with acquire(); the idle-timeout clock starts fresh from this "
                    "moment. Optional query param lease_id releases just that lease; omit it to release "
                    "every lease on this model. Safe to call on an unknown/already-expired lease_id (no-op)."
                ),
            },
            {
                "method": "POST",
                "path": "/v1/models/{name}/stop",
                "description": "stop this model if running. 409s if a lease is currently held; pass ?force=true to stop anyway.",
            },
            {"method": "GET", "path": "/v1/models/{name}/logs", "description": "recent logs for this model (?lines=200)"},
            {"method": "GET", "path": "/v1/status/summary", "description": "compact running-model summary, for dashboards/bar widgets"},
        ],
        "notes": [
            "All writes (POST) validate the full registry (including cross-references between systems/engines/gpu_groups/models) before accepting the change, and persist to registry.json on success. An invalid write returns 422 with a specific error and changes nothing.",
            "There is no delete endpoint by design: remove an entry by editing registry.json directly, then POST /v1/config/reload to apply it live (no restart needed).",
        ],
    }
