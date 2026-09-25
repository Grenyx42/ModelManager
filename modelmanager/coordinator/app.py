from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import default_registry_path, load_registry
from .manager import Manager
from .routes import router
from .scheduler import Scheduler

logger = logging.getLogger("modelmanager")


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry_path = default_registry_path()
    registry = load_registry(registry_path)
    manager = Manager(registry, registry_path=registry_path)
    app.state.manager = manager
    app.state.system_runners = {}

    for name, definition in registry.models.items():
        if definition.pinned:
            try:
                manager.ensure(name)
            except Exception:
                logger.exception("failed to auto-start pinned model %s", name)

    scheduler = Scheduler(manager)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="ModelManager", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
