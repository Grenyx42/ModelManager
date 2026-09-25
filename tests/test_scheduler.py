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
