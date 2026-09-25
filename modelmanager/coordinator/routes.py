from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import gpu_stats
from ..adapters.factory import build_runner
from .manager import ConflictError, NotFoundError

router = APIRouter()


def _manager(request: Request):
    return request.app.state.manager


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
