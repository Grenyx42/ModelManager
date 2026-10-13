import logging

import pytest

from modelmanager import access_log, lifecycle_log, remote_paths


@pytest.fixture(autouse=True)
def _clear_remote_paths_cache():
    remote_paths._home_cache.clear()
    yield
    remote_paths._home_cache.clear()


@pytest.fixture(autouse=True)
def _no_startup_grace(monkeypatch):
    # LlamaCppAdapter.start() sleeps STARTUP_GRACE_SECONDS to catch a process
    # that crashes immediately after launching, before reporting success -
    # real and needed in production, but no test should have to wait out a
    # real 1.5s for every model it starts. A test that specifically exercises
    # the crash-detection path overrides this back to a small real value.
    monkeypatch.setattr("modelmanager.adapters.llamacpp.STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr("modelmanager.adapters.process.STARTUP_GRACE_SECONDS", 0)


@pytest.fixture(autouse=True)
def _reset_access_log_handlers():
    # access_log.configure() is a no-op once *our* handler exists, so one left
    # over from a test that used a different XDG_STATE_HOME would keep writing
    # to that stale path for every later test. Only remove our own marked
    # handler - pytest attaches its own log-capture handler to every logger,
    # and that one isn't ours to touch.
    def _reset():
        logger = logging.getLogger(access_log.LOGGER_NAME)
        for handler in list(logger.handlers):
            if getattr(handler, "_modelmanager_access", False):
                logger.removeHandler(handler)
                handler.close()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_lifecycle_log_handlers():
    # Same reasoning as _reset_access_log_handlers, for the lifecycle log.
    def _reset():
        logger = logging.getLogger(lifecycle_log.LOGGER_NAME)
        for handler in list(logger.handlers):
            if getattr(handler, "_modelmanager_lifecycle", False):
                logger.removeHandler(handler)
                handler.close()

    _reset()
    yield
    _reset()
