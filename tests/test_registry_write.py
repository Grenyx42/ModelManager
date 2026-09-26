import json

import pytest

from modelmanager.coordinator.manager import Manager, RegistryValidationError, ReloadUnavailableError
from modelmanager.coordinator.state import StateStore
from tests.test_manager import FakeAdapter, FakeAdapterRegistry, build_registry


@pytest.fixture(autouse=True)
def isolate_local_state(monkeypatch, tmp_path):
    # reload_from_disk() auto-starts pinned models (build_registry()'s
    # "nomic-embed") via the real AdapterRegistry/LlamaCppAdapter/LocalRunner
    # on any test here that doesn't swap in a FakeAdapterRegistry, which
    # otherwise writes real pid/log files under ~/.local/state/modelmanager.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


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


def test_reload_without_path_raises():
    registry = build_registry()
    manager = Manager(registry, StateStore())
    with pytest.raises(ReloadUnavailableError):
        manager.reload_from_disk()


def test_reload_picks_up_hand_edited_addition(tmp_path):
    manager, path = make_manager_with_path(tmp_path)
    manager.upsert_gpu_group("gpu1", {"system": "sleek"})  # also writes the file

    data = json.loads(path.read_text())
    data["models"]["model-c"] = {
        "candidates": [{"engine": "eng", "gpu_group": "gpu1", "port": 9, "model_path": "/m/c.gguf"}]
    }
    path.write_text(json.dumps(data))

    result = manager.reload_from_disk()

    assert "model-c" in manager.registry.models
    assert "model-c" in result["models"]


def test_reload_stops_active_model_that_was_deleted(tmp_path):
    manager, path = make_manager_with_path(tmp_path)
    fake_adapter = FakeAdapter()
    manager.adapters = FakeAdapterRegistry(fake_adapter)
    manager.ensure("model-b")

    data = json.loads(path.read_text()) if path.exists() else manager.registry.model_dump(mode="json")
    del data["models"]["model-b"]
    path.write_text(json.dumps(data))

    result = manager.reload_from_disk()

    assert "model-b" not in manager.registry.models
    assert result["stopped"] == ["model-b"]
    assert fake_adapter.stop_calls == ["model-b"]


def test_reload_preserves_active_state_across_a_rename(tmp_path):
    manager, path = make_manager_with_path(tmp_path)
    fake_adapter = FakeAdapter()
    manager.adapters = FakeAdapterRegistry(fake_adapter)
    manager.ensure("model-b")

    data = json.loads(path.read_text()) if path.exists() else manager.registry.model_dump(mode="json")
    data["models"]["model-b-renamed"] = data["models"].pop("model-b")
    path.write_text(json.dumps(data))

    result = manager.reload_from_disk()

    assert result["renamed"] == {"model-b": "model-b-renamed"}
    assert result["stopped"] == []
    assert fake_adapter.stop_calls == []  # the running process was never touched
    assert "model-b" not in manager.registry.models
    assert manager.status("model-b-renamed")["active"] is True


def test_reload_does_not_rename_an_inactive_model(tmp_path):
    # model-b was never ensure()'d, so it has no running state to preserve -
    # renaming it should just look like a plain remove+add, not a "rename".
    manager, path = make_manager_with_path(tmp_path)

    data = json.loads(path.read_text()) if path.exists() else manager.registry.model_dump(mode="json")
    data["models"]["model-b-renamed"] = data["models"].pop("model-b")
    path.write_text(json.dumps(data))

    result = manager.reload_from_disk()

    assert result["renamed"] == {}
    assert result["stopped"] == []
    assert "model-b-renamed" in manager.registry.models


def test_reload_rejects_invalid_file_and_keeps_old_registry(tmp_path):
    manager, path = make_manager_with_path(tmp_path)
    manager.upsert_gpu_group("gpu1", {"system": "sleek"})
    path.write_text("{not valid json")

    with pytest.raises(RegistryValidationError):
        manager.reload_from_disk()

    assert "gpu1" in manager.registry.gpu_groups


def test_reload_missing_file_raises(tmp_path):
    registry = build_registry()
    manager = Manager(registry, StateStore(), registry_path=tmp_path / "does-not-exist.json")
    with pytest.raises(RegistryValidationError):
        manager.reload_from_disk()
