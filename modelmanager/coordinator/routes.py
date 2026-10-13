from __future__ import annotations

from fastapi import APIRouter, Request

from .. import access_log, agent_deploy, gpu_stats, lifecycle_log
from ..adapters.factory import build_runner
from ..errors import ModelManagerError, NotFoundError
from ..schema_help import API_VERSION, build_help
from .manager import Manager

router = APIRouter()


def _manager(request: Request):
    return request.app.state.manager


@router.get("/v1/help")
def help_doc():
    return build_help()


@router.get("/v1/version")
def version():
    return {"version": API_VERSION}


@router.get("/v1/access-log")
def get_access_log(lines: int = 200):
    return {"log": access_log.tail(lines)}


@router.get("/v1/lifecycle-log")
def get_lifecycle_log(lines: int = 200):
    return {"log": lifecycle_log.tail(lines)}


@router.get("/v1/config")
def get_config(request: Request):
    return _manager(request).registry.model_dump()


@router.post("/v1/config/reload")
def reload_config(request: Request):
    return _manager(request).reload_from_disk()


@router.get("/v1/config/reload-status")
def reload_status(request: Request):
    """How many times the live registry has been reloaded from disk (manual
    POST /v1/config/reload or the automatic RegistryWatcher) and when the
    last one happened, so a poller can detect "a reload just happened"
    without diffing the registry itself."""
    return _manager(request).reload_status()


@router.post("/v1/config/validate")
def validate_config(body: dict):
    """Validates a full registry document without applying it - a peer to
    the validation that already runs implicitly on every write/reload."""
    return Manager.validate_registry(body)


@router.get("/v1/doctor")
def doctor(request: Request):
    return _manager(request).doctor()


@router.post("/v1/models/{name}/ensure")
def ensure_model(name: str, request: Request):
    result = _manager(request).ensure(name)
    return {"host": result.host, "port": result.port}


@router.get("/v1/models/{name}/dry-run")
def dry_run_model(name: str, request: Request):
    return _manager(request).dry_run(name)


@router.post("/v1/models/{name}/verify")
def verify_model(name: str, request: Request, timeout: float = 30.0):
    return _manager(request).verify(name, timeout=timeout)


@router.post("/v1/models/{name}/acquire")
def acquire_model(name: str, request: Request, ttl_seconds: float | None = None):
    result = _manager(request).acquire(name, ttl_seconds)
    return {"host": result.host, "port": result.port, "lease_id": result.lease_id, "expires_in": result.expires_in}


@router.post("/v1/models/{name}/release")
def release_model(name: str, request: Request, lease_id: str | None = None):
    return _manager(request).release(name, lease_id)


@router.post("/v1/models/{name}/stop")
def stop_model(name: str, request: Request, force: bool = False):
    _manager(request).stop(name, force=force)
    return {"status": "stopped"}


@router.get("/v1/models")
def list_models(request: Request):
    return _manager(request).list_models()


@router.get("/v1/models/{name}")
def get_model(name: str, request: Request):
    return _manager(request).status(name)


@router.post("/v1/models/{name}")
def upsert_model(name: str, body: dict, request: Request):
    return _manager(request).upsert_model(name, body).model_dump()


@router.get("/v1/models/{name}/logs")
def model_logs(name: str, request: Request, lines: int = 200):
    return {"logs": _manager(request).logs(name, lines)}


@router.get("/v1/models/{name}/log-path")
def model_log_path(name: str, request: Request):
    return _manager(request).log_location(name)


@router.get("/v1/systems")
def list_systems(request: Request):
    registry = _manager(request).registry
    return {name: sys.model_dump() for name, sys in registry.systems.items()}


@router.get("/v1/systems/{name}")
def get_system(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.systems:
        raise NotFoundError(f"unknown system {name!r}")
    return registry.systems[name].model_dump()


@router.post("/v1/systems/{name}")
def upsert_system(name: str, body: dict, request: Request):
    return _manager(request).upsert_system(name, body).model_dump()


@router.get("/v1/engines")
def list_engines(request: Request):
    registry = _manager(request).registry
    return {name: eng.model_dump() for name, eng in registry.engines.items()}


@router.get("/v1/engines/{name}")
def get_engine(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.engines:
        raise NotFoundError(f"unknown engine {name!r}")
    return registry.engines[name].model_dump()


@router.post("/v1/engines/{name}")
def upsert_engine(name: str, body: dict, request: Request):
    return _manager(request).upsert_engine(name, body).model_dump()


@router.get("/v1/gpu_groups")
def list_gpu_groups(request: Request):
    registry = _manager(request).registry
    return {name: g.model_dump() for name, g in registry.gpu_groups.items()}


@router.get("/v1/gpu_groups/{name}")
def get_gpu_group(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.gpu_groups:
        raise NotFoundError(f"unknown gpu_group {name!r}")
    return registry.gpu_groups[name].model_dump()


@router.post("/v1/gpu_groups/{name}")
def upsert_gpu_group(name: str, body: dict, request: Request):
    return _manager(request).upsert_gpu_group(name, body).model_dump()


@router.get("/v1/groups")
def list_groups(request: Request):
    return _manager(request).registry.groups


@router.get("/v1/groups/{name}")
def get_group(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.groups:
        raise NotFoundError(f"unknown group {name!r}")
    return {"name": name, "models": registry.groups[name]}


@router.post("/v1/groups/{name}")
def upsert_group(name: str, body: list[str], request: Request):
    result = _manager(request).upsert_group(name, body)
    return {"name": name, "models": result}


@router.post("/v1/groups/{name}/ensure")
def ensure_group(name: str, request: Request):
    return _manager(request).ensure_group(name)


@router.get("/v1/groups/{name}/dry-run")
def dry_run_group(name: str, request: Request):
    return _manager(request).dry_run_group(name)


@router.post("/v1/groups/{name}/verify")
def verify_group(name: str, request: Request, timeout: float = 30.0):
    return _manager(request).verify_group(name, timeout=timeout)


@router.post("/v1/groups/{name}/stop")
def stop_group(name: str, request: Request, force: bool = False):
    return _manager(request).stop_group(name, force=force)


@router.get("/v1/groups/{name}/stats")
def group_stats(name: str, request: Request):
    manager = _manager(request)
    system_models = manager.group_system_models(name)
    registry = manager.registry
    runner_cache = request.app.state.system_runners
    results = []
    for system_name, model_names in system_models.items():
        if system_name not in runner_cache:
            runner_cache[system_name] = build_runner(registry.systems[system_name])
        try:
            gpus = gpu_stats.gpu_stats(runner_cache[system_name])
            results.append({"system": system_name, "models": model_names, "ok": True, "gpus": gpus, "detail": None})
        except (RuntimeError, ModelManagerError) as exc:
            # Catches HostUnreachableError/CommandTimeoutError too (raised by
            # SSHRunner itself, not just gpu_stats.py's own RuntimeError) -
            # one unreachable system must stay a per-system soft failure, not
            # take down the whole group's stats report.
            results.append({"system": system_name, "models": model_names, "ok": False, "gpus": None, "detail": str(exc)})
    return {"group": name, "results": results}


@router.post("/v1/systems/{name}/deploy-agent")
def deploy_agent_one(name: str, request: Request, force: bool = False):
    registry = _manager(request).registry
    if name not in registry.systems:
        raise NotFoundError(f"unknown system {name!r}")
    # CommandFailedError/HostUnreachableError/CommandTimeoutError all propagate
    # straight to the ModelManagerError handler - a consistent {error, code}
    # body whether the SSH connection itself failed or the remote command did.
    return agent_deploy.deploy_if_needed(name, registry.systems[name], force=force)


@router.post("/v1/deploy-agent")
def deploy_agent_all(request: Request, force: bool = False):
    registry = _manager(request).registry
    return agent_deploy.deploy_all(registry, force=force)


@router.get("/v1/systems/{name}/stats")
def system_stats(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.systems:
        raise NotFoundError(f"unknown system {name!r}")
    runner_cache = request.app.state.system_runners
    if name not in runner_cache:
        runner_cache[name] = build_runner(registry.systems[name])
    # CommandFailedError (nvidia-smi missing/failed) and HostUnreachableError/
    # CommandTimeoutError (the SSH connection itself) all propagate straight
    # to the ModelManagerError handler - one consistent {error, code} body
    # regardless of which kind of SSH problem it was.
    return {"gpus": gpu_stats.gpu_stats(runner_cache[name])}


@router.get("/v1/status/summary")
def status_summary(request: Request):
    manager = _manager(request)
    models = manager.list_models()
    running = [m for m in models if m["active"]]
    return {
        "running_count": len(running),
        "total_count": len(models),
        "running": [
            {"name": m["name"], "engine": (m["candidate"] or {}).get("engine"), "in_use": m["in_use"]}
            for m in running
        ],
    }
