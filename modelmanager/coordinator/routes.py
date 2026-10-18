from __future__ import annotations

from fastapi import APIRouter, Request

from .. import access_log, agent_deploy, gpu_stats, lifecycle_log
from ..adapters.factory import build_runner
from ..errors import ForbiddenError, ModelManagerError, NotFoundError
from ..schema_help import API_VERSION, build_help
from .manager import Manager

router = APIRouter()


def _manager(request: Request):
    return request.app.state.manager


def _grant(request: Request) -> dict | None:
    """None for the owner token (unrestricted); a {"name", "allowed_systems"}
    dict for a scoped grant (see grants.py) - set by
    coordinator.app.install_auth_middleware after checking the bearer token."""
    return getattr(request.state, "grant", None)


def _require_owner(request: Request) -> None:
    """Grants can read/operate within their allowed systems but can never
    write configuration or see cross-cutting admin data (logs of everything,
    doctor, deploy-agent) - only the owner token can."""
    if _grant(request) is not None:
        raise ForbiddenError("this operation requires the owner token, not a scoped grant")


def _allowed_systems(request: Request, registry) -> set[str] | None:
    """None means unrestricted (owner token). Otherwise the grant's
    allowed_systems intersected with systems that actually still exist (a
    grant naming a since-removed system just sees nothing for it)."""
    grant = _grant(request)
    if grant is None:
        return None
    return set(grant["allowed_systems"]) & set(registry.systems)


def _check_system_access(request: Request, systems: set[str]) -> None:
    """No-op for the owner token. For a scoped grant, requires at least one
    of `systems` to be in its allowed_systems - a model can have candidates
    on more than one system (see Manager.model_systems), so access to *any*
    of them is enough to reach the whole model."""
    grant = _grant(request)
    if grant is None:
        return
    if not systems & set(grant["allowed_systems"]):
        raise ForbiddenError(f"grant {grant['name']!r} has no access to system(s) {sorted(systems)}")


def _allowed_models(request: Request, manager: Manager, allowed: set[str] | None) -> set[str] | None:
    """None for the owner token. Otherwise every model name in the whole
    registry whose model_systems() intersects `allowed` - the set group
    operations filter their member lists against."""
    if allowed is None:
        return None
    return {name for name in manager.registry.models if manager.model_systems(name) & allowed}


def _group_visible(manager: Manager, members: list[str], allowed: set[str]) -> bool:
    """A group is visible to a scoped grant only if *every* member model is
    within its allowed systems - showing a group with some members filtered
    out would otherwise reveal that other, non-permitted models exist and
    are grouped with ones the grant can see, which is itself information
    the grant has no business learning. This only governs visibility
    (GET /v1/groups, /v1/groups/{name}, /v1/config) - group *actions*
    (ensure_group etc.) still operate best-effort per in-scope member, same
    as any other per-member failure, since those are gated by each member
    model's own scope check regardless of what the group listing shows."""
    return all(manager.model_systems(m) & allowed for m in members)


@router.get("/v1/help")
def help_doc():
    return build_help()


@router.get("/v1/version")
def version():
    return {"version": API_VERSION}


@router.get("/v1/access-log")
def get_access_log(request: Request, lines: int = 200):
    _require_owner(request)
    return {"log": access_log.tail(lines)}


@router.get("/v1/lifecycle-log")
def get_lifecycle_log(request: Request, lines: int = 200):
    _require_owner(request)
    return {"log": lifecycle_log.tail(lines)}


@router.get("/v1/config")
def get_config(request: Request):
    manager = _manager(request)
    data = manager.registry.model_dump()
    allowed = _allowed_systems(request, manager.registry)
    if allowed is None:
        return data
    data["systems"] = {n: v for n, v in data["systems"].items() if n in allowed}
    data["engines"] = {n: v for n, v in data["engines"].items() if v["system"] in allowed}
    data["gpu_groups"] = {n: v for n, v in data["gpu_groups"].items() if v["system"] in allowed}
    data["models"] = {n: v for n, v in data["models"].items() if manager.model_systems(n) & allowed}
    data["groups"] = {n: m for n, m in data["groups"].items() if _group_visible(manager, m, allowed)}
    return data


@router.post("/v1/config/reload")
def reload_config(request: Request):
    _require_owner(request)
    return _manager(request).reload_from_disk()


@router.get("/v1/config/reload-status")
def reload_status(request: Request):
    """How many times the live registry has been reloaded from disk (manual
    POST /v1/config/reload or the automatic RegistryWatcher) and when the
    last one happened, so a poller can detect "a reload just happened"
    without diffing the registry itself."""
    _require_owner(request)
    return _manager(request).reload_status()


@router.post("/v1/config/validate")
def validate_config(body: dict, request: Request):
    """Validates a full registry document without applying it - a peer to
    the validation that already runs implicitly on every write/reload."""
    _require_owner(request)
    return Manager.validate_registry(body)


@router.get("/v1/doctor")
def doctor(request: Request):
    _require_owner(request)
    return _manager(request).doctor()


@router.post("/v1/models/{name}/ensure")
def ensure_model(name: str, request: Request):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    result = manager.ensure(name)
    return {"host": result.host, "port": result.port}


@router.get("/v1/models/{name}/dry-run")
def dry_run_model(name: str, request: Request):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    return manager.dry_run(name)


@router.post("/v1/models/{name}/verify")
def verify_model(name: str, request: Request, timeout: float = 30.0):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    return manager.verify(name, timeout=timeout)


@router.post("/v1/models/{name}/acquire")
def acquire_model(name: str, request: Request, ttl_seconds: float | None = None):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    result = manager.acquire(name, ttl_seconds)
    return {"host": result.host, "port": result.port, "lease_id": result.lease_id, "expires_in": result.expires_in}


@router.post("/v1/models/{name}/release")
def release_model(name: str, request: Request, lease_id: str | None = None):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    return manager.release(name, lease_id)


@router.post("/v1/models/{name}/stop")
def stop_model(name: str, request: Request, force: bool = False):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    manager.stop(name, force=force)
    return {"status": "stopped"}


@router.get("/v1/models")
def list_models(request: Request):
    manager = _manager(request)
    results = manager.list_models()
    allowed = _allowed_systems(request, manager.registry)
    if allowed is None:
        return results
    return [m for m in results if manager.model_systems(m["name"]) & allowed]


@router.get("/v1/models/{name}")
def get_model(name: str, request: Request):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    return manager.status(name)


@router.post("/v1/models/{name}")
def upsert_model(name: str, body: dict, request: Request):
    _require_owner(request)
    return _manager(request).upsert_model(name, body).model_dump()


@router.get("/v1/models/{name}/logs")
def model_logs(name: str, request: Request, lines: int = 200):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    return {"logs": manager.logs(name, lines)}


@router.get("/v1/models/{name}/log-path")
def model_log_path(name: str, request: Request):
    manager = _manager(request)
    _check_system_access(request, manager.model_systems(name))
    return manager.log_location(name)


@router.get("/v1/systems")
def list_systems(request: Request):
    registry = _manager(request).registry
    allowed = _allowed_systems(request, registry)
    return {name: sys.model_dump() for name, sys in registry.systems.items() if allowed is None or name in allowed}


@router.get("/v1/systems/stats")
def all_systems_stats(request: Request):
    """Every (allowed) system's GPU stats in one response, instead of one
    request per system - each separate CLI invocation pays its own fresh
    TLS handshake (no connection reuse across them), which is negligible on
    localhost but adds up fast over a slower/remote link (see the bar
    panel's stats sweep, and `modelmanager stats` with no system name) -
    this collapses what used to be N sequential round trips into one.
    Must be registered before GET /v1/systems/{name} - otherwise "stats"
    would be swallowed as that route's {name} path param."""
    manager = _manager(request)
    registry = manager.registry
    allowed = _allowed_systems(request, registry)
    runner_cache = request.app.state.system_runners
    results = []
    for system_name in registry.systems:
        if allowed is not None and system_name not in allowed:
            continue
        if system_name not in runner_cache:
            runner_cache[system_name] = build_runner(registry.systems[system_name])
        try:
            gpus = gpu_stats.gpu_stats(runner_cache[system_name])
            results.append({"system": system_name, "ok": True, "gpus": gpus, "detail": None})
        except (RuntimeError, ModelManagerError) as exc:
            results.append({"system": system_name, "ok": False, "gpus": None, "detail": str(exc)})
    return {"results": results}


@router.get("/v1/systems/{name}")
def get_system(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.systems:
        raise NotFoundError(f"unknown system {name!r}")
    _check_system_access(request, {name})
    return registry.systems[name].model_dump()


@router.post("/v1/systems/{name}")
def upsert_system(name: str, body: dict, request: Request):
    _require_owner(request)
    return _manager(request).upsert_system(name, body).model_dump()


@router.get("/v1/engines")
def list_engines(request: Request):
    registry = _manager(request).registry
    allowed = _allowed_systems(request, registry)
    return {name: eng.model_dump() for name, eng in registry.engines.items() if allowed is None or eng.system in allowed}


@router.get("/v1/engines/{name}")
def get_engine(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.engines:
        raise NotFoundError(f"unknown engine {name!r}")
    _check_system_access(request, {registry.engines[name].system})
    return registry.engines[name].model_dump()


@router.post("/v1/engines/{name}")
def upsert_engine(name: str, body: dict, request: Request):
    _require_owner(request)
    return _manager(request).upsert_engine(name, body).model_dump()


@router.get("/v1/gpu_groups")
def list_gpu_groups(request: Request):
    registry = _manager(request).registry
    allowed = _allowed_systems(request, registry)
    return {name: g.model_dump() for name, g in registry.gpu_groups.items() if allowed is None or g.system in allowed}


@router.get("/v1/gpu_groups/{name}")
def get_gpu_group(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.gpu_groups:
        raise NotFoundError(f"unknown gpu_group {name!r}")
    _check_system_access(request, {registry.gpu_groups[name].system})
    return registry.gpu_groups[name].model_dump()


@router.post("/v1/gpu_groups/{name}")
def upsert_gpu_group(name: str, body: dict, request: Request):
    _require_owner(request)
    return _manager(request).upsert_gpu_group(name, body).model_dump()


@router.get("/v1/groups")
def list_groups(request: Request):
    manager = _manager(request)
    groups = manager.registry.groups
    allowed = _allowed_systems(request, manager.registry)
    if allowed is None:
        return groups
    return {name: members for name, members in groups.items() if _group_visible(manager, members, allowed)}


@router.get("/v1/groups/{name}")
def get_group(name: str, request: Request):
    manager = _manager(request)
    registry = manager.registry
    if name not in registry.groups:
        raise NotFoundError(f"unknown group {name!r}")
    members = registry.groups[name]
    allowed = _allowed_systems(request, registry)
    # Same all-or-nothing visibility rule as list_groups/get_config - a
    # grant that can't see this group in a listing shouldn't be able to
    # fetch it directly by (guessed or otherwise known) name either.
    if allowed is not None and not _group_visible(manager, members, allowed):
        raise NotFoundError(f"unknown group {name!r}")
    return {"name": name, "models": members}


@router.post("/v1/groups/{name}")
def upsert_group(name: str, body: list[str], request: Request):
    _require_owner(request)
    result = _manager(request).upsert_group(name, body)
    return {"name": name, "models": result}


@router.post("/v1/groups/{name}/ensure")
def ensure_group(name: str, request: Request):
    manager = _manager(request)
    allowed = _allowed_models(request, manager, _allowed_systems(request, manager.registry))
    return manager.ensure_group(name, allowed_models=allowed)


@router.get("/v1/groups/{name}/dry-run")
def dry_run_group(name: str, request: Request):
    manager = _manager(request)
    allowed = _allowed_models(request, manager, _allowed_systems(request, manager.registry))
    return manager.dry_run_group(name, allowed_models=allowed)


@router.post("/v1/groups/{name}/verify")
def verify_group(name: str, request: Request, timeout: float = 30.0):
    manager = _manager(request)
    allowed = _allowed_models(request, manager, _allowed_systems(request, manager.registry))
    return manager.verify_group(name, timeout=timeout, allowed_models=allowed)


@router.post("/v1/groups/{name}/stop")
def stop_group(name: str, request: Request, force: bool = False):
    manager = _manager(request)
    allowed = _allowed_models(request, manager, _allowed_systems(request, manager.registry))
    return manager.stop_group(name, force=force, allowed_models=allowed)


@router.get("/v1/groups/{name}/stats")
def group_stats(name: str, request: Request):
    manager = _manager(request)
    allowed_models = _allowed_models(request, manager, _allowed_systems(request, manager.registry))
    system_models = manager.group_system_models(name, allowed_models=allowed_models)
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
    _require_owner(request)
    registry = _manager(request).registry
    if name not in registry.systems:
        raise NotFoundError(f"unknown system {name!r}")
    # CommandFailedError/HostUnreachableError/CommandTimeoutError all propagate
    # straight to the ModelManagerError handler - a consistent {error, code}
    # body whether the SSH connection itself failed or the remote command did.
    return agent_deploy.deploy_if_needed(name, registry.systems[name], force=force)


@router.post("/v1/deploy-agent")
def deploy_agent_all(request: Request, force: bool = False):
    _require_owner(request)
    registry = _manager(request).registry
    return agent_deploy.deploy_all(registry, force=force)


@router.get("/v1/systems/{name}/stats")
def system_stats(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.systems:
        raise NotFoundError(f"unknown system {name!r}")
    _check_system_access(request, {name})
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
    allowed = _allowed_systems(request, manager.registry)
    if allowed is not None:
        models = [m for m in models if manager.model_systems(m["name"]) & allowed]
    running = [m for m in models if m["active"]]
    return {
        "running_count": len(running),
        "total_count": len(models),
        "running": [
            {"name": m["name"], "engine": (m["candidate"] or {}).get("engine"), "in_use": m["in_use"]}
            for m in running
        ],
    }
