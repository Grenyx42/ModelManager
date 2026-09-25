from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import agent_deploy
from ..config import default_registry_path, load_registry
from .manager import Manager
from .routes import router
from .scheduler import Scheduler

logger = logging.getLogger("modelmanager")


async def _deploy_agents_in_background(registry) -> None:
    # Runs off the event loop thread since it shells out (ssh/scp) per system;
    # best-effort so an offline box (e.g. coder) never delays startup.
    results = await asyncio.get_running_loop().run_in_executor(None, agent_deploy.deploy_all, registry)
    for result in results:
        logger.info("agent.ps1 deploy: %s", result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry_path = default_registry_path()
    registry = load_registry(registry_path)
    manager = Manager(registry, registry_path=registry_path)
    app.state.manager = manager
    app.state.system_runners = {}
    app.state.agent_deploy_task = asyncio.create_task(_deploy_agents_in_background(registry))

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
        app.state.agent_deploy_task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="ModelManager", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
