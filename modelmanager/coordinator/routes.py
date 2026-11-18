from __future__ import annotations

from fastapi import APIRouter, Request

from .. import access_log, agent_deploy, cpu_stats, gpu_stats, grants, lifecycle_log
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
    *currently effective* allowed_systems (see grants.effective_allowed_systems
    - excludes anything whose temporary access has expired) intersected with
    systems that actually still exist (a grant naming a since-removed system
    just sees nothing for it)."""
    grant = _grant(request)
    if grant is None:
        return None
    return grants.effective_allowed_systems(grant) & set(registry.systems)


def _system_in_scope(request: Request, systems: set[str]) -> bool:
    """True for the owner token. For a scoped grant, true iff at least one
    of `systems` is in its allowed_systems - used for the direct
    system/engine/gpu_group routes below, where a single resource maps to
    exactly one system. Model routes use is_reachable/_require_reachable
    instead, since a model's own candidates (and its dependencies) can
    span more than one system and "any one of them" isn't a strong enough
    test on its own.

    Returns a bool rather than raising (unlike the ForbiddenError this
    used to raise directly) because every caller already knows the
    resource exists and folds a False result into the exact same
    NotFoundError a genuine miss produces - an *exact* name for an
    existing-but-out-of-scope resource used to 403 while a name that
    doesn't exist at all 404'd, which is itself a distinguishable signal
    (confirmed as a real, if narrow, existence oracle by the tier-5
    adversarial smoke test against models before this was fixed there
    too - see _require_reachable). Nothing here echoes `systems` back
    either way, for the same reason: it's the resource's *backing*
    system(s), not something the caller already knows just from the name
    it asked for."""
    grant = _grant(request)
    if grant is None:
        return True
    return bool(systems & grants.effective_allowed_systems(grant))


def _allowed_models(request: Request, manager: Manager, allowed: set[str] | None) -> set[str] | None:
    """None for the owner token. Otherwise every model name in the whole
    registry that's actually reachable (see Manager.is_reachable) given
    `allowed` - not just "has some candidate in scope" but also every
    dependency, transitively. A model that can never actually be ensured
    because a required dependency sits entirely outside `allowed` is
    treated as out of scope here too, same as one with no in-scope
    candidate of its own - this is the set every other visibility/
    actionability check in this file (resolve_name's substring pool,
    _group_visible, GET /v1/config's model filtering, list/summary
    filtering, _require_reachable) ultimately derives from."""
    if allowed is None:
        return None
    return {name for name in manager.registry.models if manager.is_reachable(name, allowed)}


def _scope(request: Request, manager: Manager) -> tuple[set[str] | None, set[str] | None]:
    """Convenience wrapper for the single-model/group routes below: (None,
    None) for the owner token, otherwise (allowed_systems, allowed_models) -
    the grant's effective allowed systems and the in-scope model names
    derived from them (see _allowed_models). Both get passed straight
    through to the relevant Manager method's own `allowed_systems`/
    `allowed_models` parameters:

    - allowed_models narrows Manager.resolve_name's *substring* match pool,
      so a possibly-ambiguous `name` path param is only ever fuzzy-matched
      against models the caller can already see at all - an out-of-scope
      model's name can't leak through an AmbiguousNameError's match list.
    - allowed_systems is the finer-grained check Manager itself applies
      *after* resolving which specific candidate an action/read would
      actually touch - allowed_models (derived from Manager.is_reachable)
      only proves the model is reachable *somehow*, never that the
      candidate actually selected for this request is the one that makes
      it so.

    Must be computed once per request and reused for the access check and
    the actual Manager call below it - computing it twice and resolving
    `name` against each result independently would let the two resolutions
    disagree."""
    allowed_systems = _allowed_systems(request, manager.registry)
    return allowed_systems, _allowed_models(request, manager, allowed_systems)


def _group_visible(manager: Manager, members: list[str], allowed: set[str]) -> bool:
    """A group is visible to a scoped grant only if *every* member model is
    reachable (see Manager.is_reachable, not just "has a candidate in an
    allowed system") - showing a group with some members filtered out would
    otherwise reveal that other, non-permitted models exist and are grouped
    with ones the grant can see, which is itself information the grant has
    no business learning. Governs visibility for BOTH reads
    (GET /v1/groups, /v1/groups/{name}, /v1/config) and actions
    (ensure_group etc., /v1/groups/{name}/stats) via _require_visible_group -
    an action route must never recover membership a GET route would hide."""
    return all(manager.is_reachable(m, allowed) for m in members)


def _require_reachable(request: Request, manager: Manager, name: str) -> tuple[set[str] | None, set[str] | None]:
    """Shared gate for every /v1/models/{name}/... route, replacing a
    simpler _check_system_access(request, {systems this model's candidates
    touch}) check: resolves `name` (scoped to allowed_models for substring
    matching), then requires the result to be fully reachable
    (Manager.is_reachable) for this grant, not just "has some candidate in
    an allowed system."

    A model whose own candidate is in scope but that depends on something
    entirely outside it can never actually be ensured - gating here means
    it's treated as out of scope from the very first check, instead of only
    failing once the action's own depends_on recursion gets there - which
    would otherwise be the one signal that a hidden dependency exists at
    all. Raises NotFoundError, not ForbiddenError: an *exact* name that
    exists but is out of scope used to 403 while a substring guess for the
    same name 404'd, which a fuzzing pass against a live sandbox (tier 5,
    scripts/smoke/adversarial_tier.sh) confirmed is a real, if narrow,
    existence oracle - distinguishable response code, same as a
    distinguishable error message would be. Matching resolve_name's own
    NotFoundError wording for a true miss means every path - substring,
    case-variant, or exact name - now produces the identical body for
    "doesn't exist" and "exists but you can't see it." Must be computed
    once per request and reused for the actual Manager call below it, same
    reasoning as the old _scope()."""
    allowed_systems, allowed_models = _scope(request, manager)
    if allowed_systems is not None:
        resolved = manager.resolve_name(name, allowed_models)
        if not manager.is_reachable(resolved, allowed_systems):
            raise NotFoundError(f"unknown model {name!r}")
    return allowed_systems, allowed_models


def _require_visible_group(request: Request, manager: Manager, name: str) -> list[str]:
    """Shared existence+visibility gate for every /v1/groups/{name}/... route
    (GET and action alike) - 404 for a group that doesn't exist OR that this
    grant can't see due to mixed membership (_group_visible), so a caller
    can't recover a GET route's hidden membership just by trying an action
    or stats endpoint instead."""
    registry = manager.registry
    if name not in registry.groups:
        raise NotFoundError(f"unknown group {name!r}")
    members = registry.groups[name]
    allowed = _allowed_systems(request, registry)
    if allowed is not None and not _group_visible(manager, members, allowed):
        raise NotFoundError(f"unknown group {name!r}")
    return members


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


def _strip_out_of_scope_candidates(model: dict, allowed_engines: set[str], allowed_gpu_groups: set[str]) -> dict:
    """A model kept by is_reachable() (at least one candidate fully in
    scope, see Manager.is_reachable) may still have OTHER candidates on
    systems outside `allowed_engines`/`allowed_gpu_groups` - each candidate
    names its own absolute model_path/args/port, so dropping only the
    disallowed ones (rather than the whole model) is what actually keeps
    GET /v1/config from handing a grant full configuration detail for a
    system it has no access to. Checks the candidate's gpu_group too, not
    just its engine - same reason Manager._candidate_allowed() checks both:
    nothing in the schema requires a candidate's engine and gpu_group to
    agree on which system they're for.

    depends_on needs no filtering here - is_reachable() already guarantees
    every dependency of a model that passes it is itself reachable (and so
    present in data.models), so there's no "visible model depending on an
    invisible one" case left to defend against."""
    return {
        **model,
        "candidates": [
            c for c in model["candidates"]
            if c["engine"] in allowed_engines and c["gpu_group"] in allowed_gpu_groups
        ],
    }


@router.get("/v1/config")
def get_config(request: Request):
    manager = _manager(request)
    # The dump and every is_reachable()/_group_visible() decision made
    # against it below must see one single registry snapshot - a reload
    # landing midway through would otherwise let the dumped data and a
    # later visibility check disagree about something like a model's
    # dependency, which is exactly the kind of mismatch that could leak a
    # name this response's own filtering is supposed to keep out of it.
    with manager.consistent_registry_read():
        data = manager.registry.model_dump()
        allowed = _allowed_systems(request, manager.registry)
        if allowed is None:
            return data
        data["systems"] = {n: v for n, v in data["systems"].items() if n in allowed}
        data["engines"] = {n: v for n, v in data["engines"].items() if v["system"] in allowed}
        data["gpu_groups"] = {n: v for n, v in data["gpu_groups"].items() if v["system"] in allowed}
        allowed_engines = set(data["engines"])
        allowed_gpu_groups = set(data["gpu_groups"])
        visible_models = {n for n in data["models"] if manager.is_reachable(n, allowed)}
        data["models"] = {
            n: _strip_out_of_scope_candidates(data["models"][n], allowed_engines, allowed_gpu_groups)
            for n in visible_models
        }
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
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    result = manager.ensure(name, allowed_models, allowed_systems)
    return {"host": result.host, "port": result.port}


@router.get("/v1/models/{name}/dry-run")
def dry_run_model(name: str, request: Request):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    return manager.dry_run(name, allowed_models, allowed_systems)


@router.post("/v1/models/{name}/verify")
def verify_model(name: str, request: Request, timeout: float = 30.0):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    return manager.verify(name, timeout=timeout, allowed_models=allowed_models, allowed_systems=allowed_systems)


@router.post("/v1/models/{name}/acquire")
def acquire_model(name: str, request: Request, ttl_seconds: float | None = None):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    result = manager.acquire(name, ttl_seconds, allowed_models, allowed_systems)
    return {"host": result.host, "port": result.port, "lease_id": result.lease_id, "expires_in": result.expires_in}


@router.post("/v1/models/{name}/release")
def release_model(name: str, request: Request, lease_id: str | None = None):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    return manager.release(name, lease_id, allowed_models, allowed_systems)


@router.post("/v1/models/{name}/stop")
def stop_model(name: str, request: Request, force: bool = False):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    manager.stop(name, force=force, allowed_models=allowed_models, allowed_systems=allowed_systems)
    return {"status": "stopped"}


@router.get("/v1/models")
def list_models(request: Request):
    manager = _manager(request)
    allowed = _allowed_systems(request, manager.registry)
    results = manager.list_models(allowed_systems=allowed)
    if allowed is None:
        return results
    return [m for m in results if manager.is_reachable(m["name"], allowed)]


@router.get("/v1/models/{name}")
def get_model(name: str, request: Request):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    return manager.status(name, allowed_models, allowed_systems)


@router.post("/v1/models/{name}")
def upsert_model(name: str, body: dict, request: Request):
    _require_owner(request)
    return _manager(request).upsert_model(name, body).model_dump()


@router.get("/v1/models/{name}/logs")
def model_logs(name: str, request: Request, lines: int = 200):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    return {"logs": manager.logs(name, lines, allowed_models, allowed_systems)}


@router.get("/v1/models/{name}/log-path")
def model_log_path(name: str, request: Request):
    manager = _manager(request)
    allowed_systems, allowed_models = _require_reachable(request, manager, name)
    return manager.log_location(name, allowed_models, allowed_systems)


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
            cpu = cpu_stats.cpu_stats(runner_cache[system_name], registry.systems[system_name].os)
            results.append({"system": system_name, "ok": True, "gpus": gpus, "cpu": cpu, "detail": None})
        except (RuntimeError, ModelManagerError) as exc:
            results.append({"system": system_name, "ok": False, "gpus": None, "cpu": None, "detail": str(exc)})
    return {"results": results}


@router.get("/v1/systems/{name}")
def get_system(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.systems or not _system_in_scope(request, {name}):
        raise NotFoundError(f"unknown system {name!r}")
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
    if name not in registry.engines or not _system_in_scope(request, {registry.engines[name].system}):
        raise NotFoundError(f"unknown engine {name!r}")
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
    if name not in registry.gpu_groups or not _system_in_scope(request, {registry.gpu_groups[name].system}):
        raise NotFoundError(f"unknown gpu_group {name!r}")
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
    members = _require_visible_group(request, manager, name)
    return {"name": name, "models": members}


@router.post("/v1/groups/{name}")
def upsert_group(name: str, body: list[str], request: Request):
    _require_owner(request)
    result = _manager(request).upsert_group(name, body)
    return {"name": name, "models": result}


@router.post("/v1/groups/{name}/ensure")
def ensure_group(name: str, request: Request):
    manager = _manager(request)
    _require_visible_group(request, manager, name)
    allowed_systems, allowed_models = _scope(request, manager)
    return manager.ensure_group(name, allowed_models=allowed_models, allowed_systems=allowed_systems)


@router.get("/v1/groups/{name}/dry-run")
def dry_run_group(name: str, request: Request):
    manager = _manager(request)
    _require_visible_group(request, manager, name)
    allowed_systems, allowed_models = _scope(request, manager)
    return manager.dry_run_group(name, allowed_models=allowed_models, allowed_systems=allowed_systems)


@router.post("/v1/groups/{name}/verify")
def verify_group(name: str, request: Request, timeout: float = 30.0):
    manager = _manager(request)
    _require_visible_group(request, manager, name)
    allowed_systems, allowed_models = _scope(request, manager)
    return manager.verify_group(name, timeout=timeout, allowed_models=allowed_models, allowed_systems=allowed_systems)


@router.post("/v1/groups/{name}/stop")
def stop_group(name: str, request: Request, force: bool = False):
    manager = _manager(request)
    _require_visible_group(request, manager, name)
    allowed_systems, allowed_models = _scope(request, manager)
    return manager.stop_group(name, force=force, allowed_models=allowed_models, allowed_systems=allowed_systems)


@router.get("/v1/groups/{name}/stats")
def group_stats(name: str, request: Request):
    manager = _manager(request)
    _require_visible_group(request, manager, name)
    allowed_systems, allowed_models = _scope(request, manager)
    system_models = manager.group_system_models(name, allowed_models=allowed_models, allowed_systems=allowed_systems)
    registry = manager.registry
    runner_cache = request.app.state.system_runners
    results = []
    for system_name, model_names in system_models.items():
        if system_name not in runner_cache:
            runner_cache[system_name] = build_runner(registry.systems[system_name])
        try:
            gpus = gpu_stats.gpu_stats(runner_cache[system_name])
            cpu = cpu_stats.cpu_stats(runner_cache[system_name], registry.systems[system_name].os)
            results.append(
                {"system": system_name, "models": model_names, "ok": True, "gpus": gpus, "cpu": cpu, "detail": None}
            )
        except (RuntimeError, ModelManagerError) as exc:
            # Catches HostUnreachableError/CommandTimeoutError too (raised by
            # SSHRunner itself, not just gpu_stats.py's own RuntimeError) -
            # one unreachable system must stay a per-system soft failure, not
            # take down the whole group's stats report.
            results.append(
                {"system": system_name, "models": model_names, "ok": False, "gpus": None, "cpu": None, "detail": str(exc)}
            )
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
    if name not in registry.systems or not _system_in_scope(request, {name}):
        raise NotFoundError(f"unknown system {name!r}")
    runner_cache = request.app.state.system_runners
    if name not in runner_cache:
        runner_cache[name] = build_runner(registry.systems[name])
    # CommandFailedError (nvidia-smi missing/failed) and HostUnreachableError/
    # CommandTimeoutError (the SSH connection itself) all propagate straight
    # to the ModelManagerError handler - one consistent {error, code} body
    # regardless of which kind of SSH problem it was.
    return {
        "gpus": gpu_stats.gpu_stats(runner_cache[name]),
        "cpu": cpu_stats.cpu_stats(runner_cache[name], registry.systems[name].os),
    }


@router.get("/v1/status/summary")
def status_summary(request: Request):
    manager = _manager(request)
    allowed = _allowed_systems(request, manager.registry)
    models = manager.list_models(allowed_systems=allowed)
    if allowed is not None:
        models = [m for m in models if manager.is_reachable(m["name"], allowed)]
    running = [m for m in models if m["active"]]
    return {
        "running_count": len(running),
        "total_count": len(models),
        "running": [
            {"name": m["name"], "engine": (m["candidate"] or {}).get("engine"), "in_use": m["in_use"]}
            for m in running
        ],
    }
