import json
from datetime import datetime, timedelta, timezone

from modelmanager.coordinator.manager import Manager
from modelmanager.coordinator.scheduler import ActivityTracker, HealthChecker, RegistryWatcher, Scheduler
from modelmanager.coordinator.state import StateStore
from tests.test_manager import build_registry, make_manager


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


def test_sweep_leaves_a_non_pinned_model_with_no_idle_timeout_alone_forever():
    # idle_timeout_seconds=None means "no proactive timer" (see its
    # docstring) - the sweep must never touch it, no matter how long it's
    # been idle; it's only evicted reactively, by _ensure_loaded().
    manager, adapter = make_manager()
    manager.registry.models["model-b"].idle_timeout_seconds = None
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(days=10)

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


def test_registry_watcher_picks_up_hand_edit(tmp_path):
    path = tmp_path / "registry.json"
    registry = build_registry()
    manager = Manager(registry, StateStore(), registry_path=path)
    manager.upsert_gpu_group("gpu1", {"system": "sleek"})  # writes the file for the first time
    watcher = RegistryWatcher(manager)

    data = json.loads(path.read_text())
    data["gpu_groups"]["gpu2"] = {"system": "sleek"}
    path.write_text(json.dumps(data))

    watcher.check_once()

    assert "gpu2" in manager.registry.gpu_groups


def test_registry_watcher_does_nothing_when_file_unchanged():
    manager, _ = make_manager()  # no registry_path
    watcher = RegistryWatcher(manager)

    watcher.check_once()  # must not raise ReloadUnavailableError


def test_health_checker_restarts_a_crashed_active_model():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    adapter.running["model-b"] = False  # simulate the process dying, without going through stop()

    HealthChecker(manager).check_once()

    assert adapter.start_calls == ["model-b", "model-b"]
    assert manager.status("model-b")["active"] is True


def test_health_checker_leaves_a_healthy_active_model_alone():
    manager, adapter = make_manager()
    manager.ensure("model-b")

    HealthChecker(manager).check_once()

    assert adapter.start_calls == ["model-b"]


def test_health_checker_ignores_inactive_models():
    manager, adapter = make_manager()

    HealthChecker(manager).check_once()

    assert adapter.start_calls == []


def test_activity_tracker_refreshes_idle_clock_while_engine_is_busy():
    """The bug this exists for: traffic goes straight to the engine, so
    last_used would otherwise sit frozen at the ensure() that started the
    model and the sweep would evict it mid-workload."""
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=59)
    adapter.busy_signal["model-b"] = True

    ActivityTracker(manager).check_once()

    assert manager.state.get("model-b").idle_seconds() < 1
    Scheduler(manager).sweep_once()
    assert adapter.stop_calls == []


def test_activity_tracker_leaves_idle_clock_alone_when_engine_is_quiet():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    stale = datetime.now(timezone.utc) - timedelta(seconds=59)
    state.last_used = stale
    adapter.busy_signal["model-b"] = False

    ActivityTracker(manager).check_once()

    assert manager.state.get("model-b").last_used == stale


def test_activity_tracker_leaves_idle_clock_alone_when_engine_cannot_tell():
    """None means no signal - fall back to the old call-based clock rather
    than treating a silent engine as either busy or idle."""
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    stale = datetime.now(timezone.utc) - timedelta(seconds=59)
    state.last_used = stale
    adapter.busy_signal["model-b"] = None

    ActivityTracker(manager).check_once()

    assert manager.state.get("model-b").last_used == stale


def test_activity_tracker_skips_models_that_cannot_be_evicted():
    """Pinned models and models with no timeout are never evicted, so probing
    them would just be a wasted request every tick."""
    manager, adapter = make_manager()
    manager.ensure("nomic-embed")

    ActivityTracker(manager).check_once()

    assert "nomic-embed" not in adapter.busy_calls


def test_activity_tracker_ignores_models_that_are_not_running():
    manager, adapter = make_manager()

    ActivityTracker(manager).check_once()

    assert adapter.busy_calls == []


def test_sweep_does_not_evict_a_model_that_is_mid_request():
    """Closes the sampling gap: a request that lands between the tracker's
    last tick and this sweep must not be killed mid-generation."""
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)
    adapter.busy_signal["model-b"] = True

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == []
    assert manager.state.get("model-b").idle_seconds() < 1


def test_sweep_still_evicts_when_engine_reports_no_activity():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)
    adapter.busy_signal["model-b"] = False

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == ["model-b"]


def test_sweep_still_evicts_when_engine_has_no_activity_signal():
    """Unchanged behaviour for engines that can't report activity."""
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)
    adapter.busy_signal["model-b"] = None

    Scheduler(manager).sweep_once()

    assert adapter.stop_calls == ["model-b"]


def test_probe_busy_survives_a_throwing_adapter():
    """A broken probe must degrade to "no opinion", not break the sweep."""
    manager, adapter = make_manager()
    manager.ensure("model-b")
    state = manager.state.get("model-b")
    state.last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    def boom(*args, **kwargs):
        raise RuntimeError("probe exploded")

    adapter.busy = boom

    assert manager.probe_busy("model-b") is None
    Scheduler(manager).sweep_once()
    assert adapter.stop_calls == ["model-b"]
