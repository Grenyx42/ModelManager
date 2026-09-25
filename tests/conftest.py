import pytest

from modelmanager import remote_paths


@pytest.fixture(autouse=True)
def _clear_remote_paths_cache():
    remote_paths._home_cache.clear()
    yield
    remote_paths._home_cache.clear()
