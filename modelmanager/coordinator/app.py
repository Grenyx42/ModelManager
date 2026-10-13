from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .. import access_log, agent_deploy, auth, lifecycle_log
from ..config import Registry, default_backup_path, default_registry_path, load_registry
from ..errors import ModelManagerError
from .manager import Manager
from .routes import router
from .scheduler import ActivityTracker, HealthChecker, RegistryWatcher, Scheduler

logger = logging.getLogger("modelmanager")


async def _deploy_agents_in_background(registry) -> None:
    # Runs off the event loop thread since it shells out (ssh/scp) per system;
    # best-effort so an offline remote box never delays startup.
    results = await asyncio.get_running_loop().run_in_executor(None, agent_deploy.deploy_all, registry)
    for result in results:
        logger.info("agent.ps1 deploy: %s", result)


def load_registry_or_empty(registry_path: Path) -> tuple[Registry, str | None]:
    """Best-effort startup load: a registry.json broken by a hand edit (bad
    JSON, a validation error) shouldn't crash the whole coordinator - that
    just leaves it unreachable, which tells a caller nothing about *why*.

    Falls back to the last known-good snapshot (see Manager.save_backup) if
    one exists, rather than an empty registry - a hand-edit gone wrong at the
    exact moment of a restart shouldn't lose every running model's config
    just because there's no in-memory state left to protect it, the way a
    live reload already protects itself. Only truly falls back to empty when
    there's no backup to recover from (e.g. the very first run).

    Either way the REST API and RegistryWatcher stay up, the failure is
    visible via GET /v1/config/reload-status like any other failed reload,
    and the live file is picked up automatically the moment it's fixed
    (registry_changed_on_disk() still sees it as different from what was
    last read)."""
    try:
        return load_registry(registry_path), None
    except (FileNotFoundError, json.JSONDecodeError, ValidationError) as exc:
        backup_path = default_backup_path(registry_path)
        if backup_path.exists():
            try:
                backup_registry = load_registry(backup_path)
            except (FileNotFoundError, json.JSONDecodeError, ValidationError):
                pass
            else:
                return backup_registry, (
                    f"registry.json is invalid ({exc}); serving last known good config from {backup_path.name}"
                )
        return Registry(systems={}, gpu_groups={}, engines={}, models={}), str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry_path = default_registry_path()
    registry, startup_error = load_registry_or_empty(registry_path)
    manager = Manager(registry, registry_path=registry_path)
    if startup_error:
        manager.last_reload_error = startup_error
        logger.error("registry.json failed to load at startup (%s)", startup_error)
    else:
        # Loaded cleanly on its own merits - worth snapshotting even if
        # nothing's been reloaded/upserted yet this boot, so a *subsequent*
        # restart has something to recover to if registry.json breaks later.
        manager.save_backup()
    app.state.manager = manager
    app.state.system_runners = {}
    app.state.agent_deploy_task = asyncio.create_task(_deploy_agents_in_background(registry))

    reattached = manager.reconcile_running_state()
    if reattached:
        logger.info("reattached already-running models after restart: %s", reattached)

    for name, definition in registry.models.items():
        if definition.pinned:
            try:
                manager.ensure(name)
            except Exception:
                logger.exception("failed to auto-start pinned model %s", name)

    scheduler = Scheduler(manager)
    scheduler.start()
    app.state.scheduler = scheduler

    registry_watcher = RegistryWatcher(manager)
    registry_watcher.start()
    app.state.registry_watcher = registry_watcher

    health_checker = HealthChecker(manager)
    health_checker.start()
    app.state.health_checker = health_checker

    activity_tracker = ActivityTracker(manager)
    activity_tracker.start()
    app.state.activity_tracker = activity_tracker
    try:
        yield
    finally:
        scheduler.stop()
        registry_watcher.stop()
        health_checker.stop()
        activity_tracker.stop()
        app.state.agent_deploy_task.cancel()


def install_access_log_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def log_access(request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        client = f"{request.client.host}:{request.client.port}" if request.client else "-"
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query
        access_log.record(client, request.method, path, response.status_code, duration_ms)
        return response


def install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(ModelManagerError)
    async def handle_model_manager_error(request: Request, exc: ModelManagerError):
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc), "code": exc.code})


def install_auth_middleware(app: FastAPI) -> None:
    """Every request needs `Authorization: Bearer <token>` matching
    auth.read_token() - no path is exempt (including /v1/help), by design:
    one rule, nothing to get wrong about which routes are "safe" to leave
    open. Built as a plain JSONResponse rather than raising ModelManagerError
    because middleware runs outside install_error_handler's reach in some
    Starlette versions - returning directly here is simpler than relying on
    that to catch it.

    Reads the expected token fresh on every request instead of caching it at
    startup, so `modelmanager auth rotate` takes effect immediately with no
    coordinator restart - the file is tiny and local, the extra read is
    negligible next to an SSH-backed ensure()/stop() call.

    No token configured at all (vs. a wrong one) is treated as a server
    misconfiguration, not a client auth failure - fails every request with a
    distinct 500/AUTH_NOT_CONFIGURED rather than quietly accepting everything
    (what today's no-auth behavior amounts to) or refusing to start the
    service outright (which would crash-loop anyone who upgrades without
    re-running scripts/setup.sh first).
    """
    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        expected = auth.read_token()
        if expected is None:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "no REST API auth token configured - run `modelmanager auth rotate` "
                             "(or re-run scripts/setup.sh), then retry; no restart needed",
                    "code": "AUTH_NOT_CONFIGURED",
                },
            )
        scheme, _, provided = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not provided or not hmac.compare_digest(provided, expected):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "missing or invalid bearer token - pass `Authorization: Bearer <token>` "
                             "(see `modelmanager auth show`)",
                    "code": "UNAUTHORIZED",
                },
            )
        return await call_next(request)


def create_app() -> FastAPI:
    access_log.configure()
    lifecycle_log.configure()
    app = FastAPI(title="ModelManager", lifespan=lifespan)
    app.include_router(router)
    install_access_log_middleware(app)
    install_auth_middleware(app)
    install_error_handler(app)
    return app


app = create_app()
