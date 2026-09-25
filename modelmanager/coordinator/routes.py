from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import gpu_stats
from ..adapters.factory import build_runner
from ..schema_help import build_help
from .manager import ConflictError, NotFoundError, RegistryValidationError, ReloadUnavailableError

router = APIRouter()


def _manager(request: Request):
    return request.app.state.manager


@router.get("/v1/help")
def help_doc():
    return build_help()


@router.get("/v1/config")
def get_config(request: Request):
    return _manager(request).registry.model_dump()


@router.post("/v1/config/reload")
def reload_config(request: Request):
    manager = _manager(request)
    try:
        return manager.reload_from_disk()
    except ReloadUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RegistryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/models/{name}/ensure")
def ensure_model(name: str, request: Request):
    manager = _manager(request)
    try:
        result = manager.ensure(name)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"host": result.host, "port": result.port}


@router.post("/v1/models/{name}/stop")
def stop_model(name: str, request: Request):
    manager = _manager(request)
    try:
        manager.stop(name)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "stopped"}


@router.get("/v1/models")
def list_models(request: Request):
    return _manager(request).list_models()


@router.get("/v1/models/{name}")
def get_model(name: str, request: Request):
    manager = _manager(request)
    try:
        return manager.status(name)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/models/{name}")
def upsert_model(name: str, body: dict, request: Request):
    manager = _manager(request)
    try:
        result = manager.upsert_model(name, body)
    except RegistryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.model_dump()


@router.get("/v1/models/{name}/logs")
def model_logs(name: str, request: Request, lines: int = 200):
    manager = _manager(request)
    try:
        text = manager.logs(name, lines)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"logs": text}


@router.get("/v1/systems")
def list_systems(request: Request):
    registry = _manager(request).registry
    return {name: sys.model_dump() for name, sys in registry.systems.items()}


@router.get("/v1/systems/{name}")
def get_system(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.systems:
        raise HTTPException(status_code=404, detail=f"unknown system {name!r}")
    return registry.systems[name].model_dump()


@router.post("/v1/systems/{name}")
def upsert_system(name: str, body: dict, request: Request):
    try:
        result = _manager(request).upsert_system(name, body)
    except RegistryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.model_dump()


@router.get("/v1/engines")
def list_engines(request: Request):
    registry = _manager(request).registry
    return {name: eng.model_dump() for name, eng in registry.engines.items()}


@router.get("/v1/engines/{name}")
def get_engine(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.engines:
        raise HTTPException(status_code=404, detail=f"unknown engine {name!r}")
    return registry.engines[name].model_dump()


@router.post("/v1/engines/{name}")
def upsert_engine(name: str, body: dict, request: Request):
    try:
        result = _manager(request).upsert_engine(name, body)
    except RegistryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.model_dump()


@router.get("/v1/gpu_groups")
def list_gpu_groups(request: Request):
    registry = _manager(request).registry
    return {name: g.model_dump() for name, g in registry.gpu_groups.items()}


@router.get("/v1/gpu_groups/{name}")
def get_gpu_group(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.gpu_groups:
        raise HTTPException(status_code=404, detail=f"unknown gpu_group {name!r}")
    return registry.gpu_groups[name].model_dump()


@router.post("/v1/gpu_groups/{name}")
def upsert_gpu_group(name: str, body: dict, request: Request):
    try:
        result = _manager(request).upsert_gpu_group(name, body)
    except RegistryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.model_dump()


@router.get("/v1/systems/{name}/stats")
def system_stats(name: str, request: Request):
    registry = _manager(request).registry
    if name not in registry.systems:
        raise HTTPException(status_code=404, detail=f"unknown system {name!r}")
    runner_cache = request.app.state.system_runners
    if name not in runner_cache:
        runner_cache[name] = build_runner(registry.systems[name])
    try:
        return {"gpus": gpu_stats.gpu_stats(runner_cache[name])}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/v1/status/summary")
def status_summary(request: Request):
    manager = _manager(request)
    models = manager.list_models()
    running = [m for m in models if m["active"]]
    return {
        "running_count": len(running),
        "total_count": len(models),
        "running": [{"name": m["name"], "engine": (m["candidate"] or {}).get("engine")} for m in running],
    }
