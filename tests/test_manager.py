import pytest

from modelmanager.adapters.base import AdapterStatus, EngineAdapter
from modelmanager.config import Candidate, EngineConfig, GpuGroupConfig, ModelDefinition, Registry, SystemConfig
from modelmanager.coordinator.manager import ConflictError, Manager, NotFoundError
from modelmanager.coordinator.state import StateStore


class FakeAdapter(EngineAdapter):
    def __init__(self):
        self.running: dict[str, bool] = {}
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []

    def start(self, definition_name, candidate):
        self.start_calls.append(definition_name)
        self.running[definition_name] = True

    def stop(self, definition_name, candidate):
        self.stop_calls.append(definition_name)
        self.running[definition_name] = False

    def status(self, definition_name, candidate):
        return AdapterStatus(running=self.running.get(definition_name, False))

    def logs(self, definition_name, candidate, lines=200):
        return "fake logs"


class FakeAdapterRegistry:
    def __init__(self, adapter: FakeAdapter):
        self.adapter = adapter

    def get(self, engine_name):
        return self.adapter

    def invalidate(self, engine_name):
        pass

    def clear(self):
        pass


def build_registry() -> Registry:
    return Registry(
        systems={"sleek": SystemConfig(os="linux", control="local")},
        gpu_groups={"gpu0": GpuGroupConfig(system="sleek")},
        engines={"eng": EngineConfig(system="sleek", type="llama.cpp", binary="/bin/true")},
        models={
            "nomic-embed": ModelDefinition(
                pinned=True,
                candidates=[Candidate(engine="eng", gpu_group="gpu0", port=1, model_path="/m/a.gguf")],
            ),
            "model-b": ModelDefinition(
                idle_timeout_seconds=60,
                candidates=[Candidate(engine="eng", gpu_group="gpu0", port=2, model_path="/m/b.gguf")],
            ),
        },
    )


def make_manager():
    registry = build_registry()
    manager = Manager(registry, StateStore())
    fake_adapter = FakeAdapter()
    manager.adapters = FakeAdapterRegistry(fake_adapter)
    return manager, fake_adapter


def test_ensure_starts_model():
    manager, adapter = make_manager()
    result = manager.ensure("nomic-embed")
    assert result.port == 1
    assert adapter.start_calls == ["nomic-embed"]


def test_ensure_is_idempotent_when_already_running():
    manager, adapter = make_manager()
    manager.ensure("nomic-embed")
    manager.ensure("nomic-embed")
    assert adapter.start_calls == ["nomic-embed"]


def test_ensure_unknown_model_raises():
    manager, _ = make_manager()
    with pytest.raises(NotFoundError):
        manager.ensure("does-not-exist")


def test_ensure_conflicts_with_pinned_occupant():
    manager, adapter = make_manager()
    manager.ensure("nomic-embed")
    with pytest.raises(ConflictError, match="pinned"):
        manager.ensure("model-b")
    assert adapter.start_calls == ["nomic-embed"]


def test_ensure_conflicts_when_occupant_not_idle_long_enough():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    with pytest.raises(ConflictError):
        manager.ensure("nomic-embed")


def test_ensure_swaps_once_occupant_is_idle(monkeypatch):
    manager, adapter = make_manager()
    manager.ensure("model-b")

    state = manager.state.get("model-b")
    from datetime import datetime, timedelta, timezone

    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    result = manager.ensure("nomic-embed")

    assert result.port == 1
    assert adapter.stop_calls == ["model-b"]
    assert adapter.running["model-b"] is False
    assert adapter.running["nomic-embed"] is True


def test_stop_is_noop_when_not_active():
    manager, adapter = make_manager()
    manager.stop("model-b")
    assert adapter.stop_calls == []


def test_list_models_reports_status():
    manager, adapter = make_manager()
    manager.ensure("nomic-embed")
    statuses = {m["name"]: m for m in manager.list_models()}
    assert statuses["nomic-embed"]["active"] is True
    assert statuses["model-b"]["active"] is False


def test_status_reports_gpu_groups_even_when_inactive():
    manager, _ = make_manager()
    status = manager.status("model-b")
    assert status["active"] is False
    assert status["gpu_groups"] == ["gpu0"]


def test_acquire_returns_lease_id_and_marks_in_use():
    manager, adapter = make_manager()
    result = manager.acquire("model-b")

    assert result.port == 2
    assert result.lease_id
    status = manager.status("model-b")
    assert status["in_use"] is True
    assert status["lease_count"] == 1


def test_acquire_blocks_swap_even_past_idle_timeout():
    manager, adapter = make_manager()
    manager.acquire("model-b")

    state = manager.state.get("model-b")
    from datetime import datetime, timedelta, timezone

    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)  # past idle_timeout_seconds=60

    with pytest.raises(ConflictError, match="active use"):
        manager.ensure("nomic-embed")
    assert adapter.stop_calls == []


def test_release_then_idle_allows_swap():
    manager, adapter = make_manager()
    result = manager.acquire("model-b")
    manager.release("model-b", result.lease_id)

    state = manager.state.get("model-b")
    from datetime import datetime, timedelta, timezone

    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    manager.ensure("nomic-embed")
    assert adapter.stop_calls == ["model-b"]


def test_release_unknown_lease_id_is_a_noop():
    manager, _ = make_manager()
    manager.acquire("model-b")
    result = manager.release("model-b", "not-a-real-lease")
    assert result == {"released": 0, "remaining_leases": 1}


def test_release_with_no_lease_id_clears_all():
    manager, _ = make_manager()
    manager.acquire("model-b")
    manager.acquire("model-b")
    result = manager.release("model-b")
    assert result == {"released": 2, "remaining_leases": 0}
    assert manager.status("model-b")["in_use"] is False


def test_release_unknown_model_raises():
    manager, _ = make_manager()
    with pytest.raises(NotFoundError):
        manager.release("does-not-exist")


def test_stop_raises_conflict_when_leased():
    manager, adapter = make_manager()
    manager.acquire("model-b")
    with pytest.raises(ConflictError):
        manager.stop("model-b")
    assert adapter.stop_calls == []


def test_stop_force_true_stops_leased_model():
    manager, adapter = make_manager()
    manager.acquire("model-b")
    manager.stop("model-b", force=True)
    assert adapter.stop_calls == ["model-b"]
    assert manager.status("model-b")["in_use"] is False


def test_acquire_lease_expires_and_allows_swap(monkeypatch):
    manager, adapter = make_manager()
    manager.acquire("model-b", ttl_seconds=1)

    state = manager.state.get("model-b")
    from datetime import datetime, timedelta, timezone

    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    import modelmanager.coordinator.state as state_module

    monkeypatch.setattr(state_module.time, "monotonic", lambda: 1e12)

    manager.ensure("nomic-embed")
    assert adapter.stop_calls == ["model-b"]
