from datetime import datetime, timedelta, timezone

from modelmanager.coordinator.scheduler import Scheduler
from tests.test_manager import make_manager


def test_sweep_stops_idle_non_pinned_model():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == ["model-b"]
    assert manager.state.get("model-b").is_active is False


def test_sweep_leaves_pinned_model_alone():
    manager, adapter = make_manager()
    manager.ensure("nomic-embed")
    state = manager.state.get("nomic-embed")
    state.last_used = datetime.now(timezone.utc) - timedelta(hours=10)

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == []


def test_sweep_leaves_recently_used_model_running():
    manager, adapter = make_manager()
    manager.ensure("model-b")

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == []


def test_sweep_never_evicts_a_leased_model_even_past_idle_timeout():
    manager, adapter = make_manager()
    manager.acquire("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == []
    assert manager.status("model-b")["active"] is True


def test_sweep_evicts_once_lease_expires(monkeypatch):
    manager, adapter = make_manager()
    manager.acquire("model-b", ttl_seconds=1)
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    import modelmanager.coordinator.state as state_module

    monkeypatch.setattr(state_module.time, "monotonic", lambda: 1e12)

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == ["model-b"]
