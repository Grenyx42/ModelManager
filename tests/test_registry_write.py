import json

import pytest

from modelmanager.coordinator.manager import Manager, RegistryValidationError
from modelmanager.coordinator.state import StateStore
from tests.test_manager import build_registry


def make_manager_with_path(tmp_path):
    registry = build_registry()
    path = tmp_path / "registry.json"
    manager = Manager(registry, StateStore(), registry_path=path)
    return manager, path


def test_upsert_model_adds_new_definition(tmp_path):
    manager, path = make_manager_with_path(tmp_path)

    result = manager.upsert_model(
        "model-c",
        {
            "idle_timeout_seconds": 120,
            "candidates": [{"engine": "eng", "gpu_group": "gpu0", "port": 3, "model_path": "/m/c.gguf"}],
        },
    )

    assert result.idle_timeout_seconds == 120
    assert "model-c" in manager.registry.models
    on_disk = json.loads(path.read_text())
    assert "model-c" in on_disk["models"]


def test_upsert_model_unknown_engine_rejected(tmp_path):
    manager, path = make_manager_with_path(tmp_path)

    with pytest.raises(RegistryValidationError):
        manager.upsert_model(
            "model-c",
            {"candidates": [{"engine": "does-not-exist", "gpu_group": "gpu0", "port": 3, "model_path": "/m/c.gguf"}]},
        )

    assert "model-c" not in manager.registry.models
    assert not path.exists()


def test_upsert_system_clears_adapter_cache(tmp_path):
    manager, _ = make_manager_with_path(tmp_path)
    adapter_before = manager.adapters.get("eng")

    manager.upsert_system("sleek", {"os": "linux", "control": "local"})

    assert "eng" not in manager.adapters._cache
    adapter_after = manager.adapters.get("eng")
    assert adapter_after is not adapter_before


def test_upsert_engine_invalidates_only_that_engine(tmp_path):
    manager, _ = make_manager_with_path(tmp_path)
    manager.upsert_engine("other", {"system": "sleek", "type": "llama.cpp", "binary": "/bin/true"})
    cached_eng = manager.adapters.get("eng")

    manager.upsert_engine("eng", {"system": "sleek", "type": "llama.cpp", "binary": "/bin/false"})

    assert "eng" not in manager.adapters._cache
    assert manager.registry.engines["eng"].binary == "/bin/false"


def test_second_write_preserves_first(tmp_path):
    manager, path = make_manager_with_path(tmp_path)

    manager.upsert_gpu_group("gpu1", {"system": "sleek"})
    manager.upsert_gpu_group("gpu2", {"system": "sleek"})

    on_disk = json.loads(path.read_text())
    assert set(on_disk["gpu_groups"]) == {"gpu0", "gpu1", "gpu2"}
