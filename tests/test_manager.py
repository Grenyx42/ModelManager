import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from modelmanager.adapters.base import AdapterStatus, EngineAdapter
from modelmanager.config import Candidate, EngineConfig, GpuGroupConfig, ModelDefinition, Registry, SystemConfig
from modelmanager.coordinator.manager import AmbiguousNameError, ConflictError, Manager, NotFoundError
from modelmanager.coordinator.state import StateStore


class FakeAdapter(EngineAdapter):
    def __init__(self):
        self.running: dict[str, bool] = {}
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        # Per-model activity signal handed back from busy(): True/False is a
        # real answer, None means "this engine can't tell" (the default).
        self.busy_signal: dict[str, bool | None] = {}
        self.busy_calls: list[str] = []

    def start(self, definition_name, candidate):
        self.start_calls.append(definition_name)
        self.running[definition_name] = True
        return candidate.port

    def stop(self, definition_name, candidate):
        self.stop_calls.append(definition_name)
        self.running[definition_name] = False

    def status(self, definition_name, candidate):
        return AdapterStatus(running=self.running.get(definition_name, False))

    def busy(self, definition_name, candidate, host, port):
        self.busy_calls.append(definition_name)
        return self.busy_signal.get(definition_name)

    def logs(self, definition_name, candidate, lines=200):
        return "fake logs"

    def log_paths(self, definition_name, candidate):
        return [f"/fake/logs/{definition_name}.log"]


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
            "model-z": ModelDefinition(
                idle_timeout_seconds=60,
                candidates=[Candidate(engine="eng", gpu_group="gpu0", port=3, model_path="/m/z.gguf")],
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


def test_resolve_name_exact_match_wins_immediately():
    manager, _ = make_manager()
    assert manager.resolve_name("nomic-embed") == "nomic-embed"


def test_resolve_name_unique_substring_match():
    manager, _ = make_manager()
    assert manager.resolve_name("nomic") == "nomic-embed"
    assert manager.resolve_name("NOMIC") == "nomic-embed"  # case-insensitive


def test_resolve_name_ambiguous_substring_raises():
    manager, _ = make_manager()
    with pytest.raises(AmbiguousNameError, match="model-b.*model-z|model-z.*model-b"):
        manager.resolve_name("model-")


def test_resolve_name_no_match_raises_not_found():
    manager, _ = make_manager()
    with pytest.raises(NotFoundError):
        manager.resolve_name("does-not-exist")


def test_model_systems_returns_the_single_system_for_a_simple_model():
    manager, _ = make_manager()

    assert manager.model_systems("nomic-embed") == {"sleek"}


def test_model_systems_unions_across_every_candidate():
    registry = Registry(
        systems={
            "sys-a": SystemConfig(os="linux", control="local"),
            "sys-b": SystemConfig(os="linux", control="local"),
        },
        gpu_groups={"gpu-a": GpuGroupConfig(system="sys-a"), "gpu-b": GpuGroupConfig(system="sys-b")},
        engines={
            "eng-a": EngineConfig(system="sys-a", type="llama.cpp", binary="/bin/true"),
            "eng-b": EngineConfig(system="sys-b", type="llama.cpp", binary="/bin/true"),
        },
        models={
            "split-model": ModelDefinition(
                candidates=[
                    Candidate(engine="eng-a", gpu_group="gpu-a", port=1, model_path="/m/a.gguf"),
                    Candidate(engine="eng-b", gpu_group="gpu-b", port=2, model_path="/m/b.gguf"),
                ],
            ),
        },
    )
    manager = Manager(registry, StateStore())

    assert manager.model_systems("split-model") == {"sys-a", "sys-b"}


def test_ensure_accepts_a_unique_partial_name():
    manager, adapter = make_manager()
    result = manager.ensure("nomic")
    assert result.port == 1
    assert adapter.start_calls == ["nomic-embed"]
    # state is tracked under the resolved canonical name, not the partial one typed
    assert manager.state.get("nomic-embed").is_active is True


def test_verify_llamacpp_success(monkeypatch):
    manager, adapter = make_manager()

    def fake_post(url, json, timeout):
        assert url == "http://localhost:2/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = manager.verify("model-b")

    assert result["ok"] is True
    assert result["engine_type"] == "llama.cpp"
    assert result["host"] == "localhost"
    assert result["port"] == 2
    assert isinstance(result["latency_ms"], float)


def test_verify_ollama_success(monkeypatch):
    manager, adapter = make_manager()
    manager.registry.engines["eng-ollama"] = EngineConfig(system="sleek", type="ollama", base_url="http://x")
    manager.registry.models["ollama-model"] = ModelDefinition(
        idle_timeout_seconds=60,
        candidates=[Candidate(engine="eng-ollama", gpu_group="gpu0", port=11434, model_ref="nomic-embed-text")],
    )

    posted = {}

    def fake_post(url, json, timeout):
        posted["url"] = url
        posted["json"] = json
        return httpx.Response(200, json={"response": "OK"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = manager.verify("ollama-model")

    assert result["ok"] is True
    assert posted["url"] == "http://localhost:11434/api/generate"
    assert posted["json"]["model"] == "nomic-embed-text"


def test_verify_reports_failure_without_raising(monkeypatch):
    manager, adapter = make_manager()

    def fake_post(url, json, timeout):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = manager.verify("model-b")

    assert result["ok"] is False
    assert "connection refused" in result["detail"]


def test_verify_unknown_engine_type_skips_request():
    manager, adapter = make_manager()
    manager.registry.engines["eng-process"] = EngineConfig(
        system="sleek", type="process", binary="/usr/bin/python3"
    )
    manager.registry.models["proxy-model"] = ModelDefinition(
        idle_timeout_seconds=60,
        candidates=[Candidate(engine="eng-process", gpu_group="gpu0", port=9, args=["-u", "proxy.py"])],
    )

    result = manager.verify("proxy-model")

    assert result["ok"] is None
    assert "no standard inference API" in result["detail"]


def test_verify_uses_health_check_override_for_process_engine(monkeypatch):
    from modelmanager.config import HealthCheck

    manager, adapter = make_manager()
    manager.registry.engines["eng-process"] = EngineConfig(
        system="sleek", type="process", binary="/usr/bin/python3"
    )
    manager.registry.models["proxy-model"] = ModelDefinition(
        idle_timeout_seconds=60,
        candidates=[
            Candidate(
                engine="eng-process",
                gpu_group="gpu0",
                port=9,
                args=["-u", "proxy.py"],
                health_check=HealthCheck(method="GET", path="/health"),
            )
        ],
    )

    def fake_get(url, timeout):
        assert url == "http://localhost:9/health"
        return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    result = manager.verify("proxy-model")

    assert result["ok"] is True
    assert isinstance(result["latency_ms"], float)


def test_verify_health_check_override_can_be_post_with_body(monkeypatch):
    from modelmanager.config import HealthCheck

    manager, adapter = make_manager()

    def fake_post(url, json, timeout):
        assert url == "http://localhost:2/custom"
        assert json == {"ping": True}
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    manager.registry.models["model-b"].candidates[0].health_check = HealthCheck(
        method="POST", path="/custom", body={"ping": True}
    )

    result = manager.verify("model-b")

    assert result["ok"] is True


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


def test_ensure_evicts_occupant_immediately_when_its_idle_timeout_is_none():
    # idle_timeout_seconds=None means "never evicted by the proactive idle
    # sweep", not "never evictable" - it still yields the instant something
    # else needs its gpu_group, with no idle grace period to wait out.
    manager, adapter = make_manager()
    manager.registry.models["model-b"].idle_timeout_seconds = None
    manager.ensure("model-b")

    result = manager.ensure("nomic-embed")

    assert result.port == 1
    assert adapter.stop_calls == ["model-b"]


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


def test_log_location_reports_system_and_paths():
    manager, adapter = make_manager()

    location = manager.log_location("model-b")

    assert location == {
        "system": "sleek",
        "control": "local",
        "os": "linux",
        "ssh_host": None,
        "paths": ["/fake/logs/model-b.log"],
    }


def test_log_location_unknown_model_raises():
    manager, _ = make_manager()
    with pytest.raises(NotFoundError):
        manager.log_location("does-not-exist")


def test_reconcile_running_state_reattaches_a_model_already_running():
    manager, adapter = make_manager()
    # Simulate a coordinator restart: the process is running (adapter says
    # so) but this Manager's state has no memory of it ever being started.
    adapter.running["model-b"] = True

    reattached = manager.reconcile_running_state()

    assert reattached == {"model-b": 0}
    assert manager.status("model-b")["active"] is True
    assert adapter.start_calls == []  # must not have launched a second instance


def test_reconcile_running_state_leaves_actually_stopped_models_alone():
    manager, adapter = make_manager()

    reattached = manager.reconcile_running_state()

    assert reattached == {}
    assert manager.status("model-b")["active"] is False


def test_reconcile_running_state_skips_already_active_models():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    adapter.start_calls.clear()

    reattached = manager.reconcile_running_state()

    assert reattached == {}
    assert adapter.start_calls == []


def test_ensure_group_best_effort_reports_each_member():
    manager, adapter = make_manager()
    # nomic-embed, model-b and model-z all share gpu0, so once nomic-embed
    # (pinned) takes it, the other members can never win the slot - a
    # realistic "one member blocks the rest" case for a best-effort group.
    manager.registry.groups["g1"] = ["nomic-embed", "model-b", "model-z"]

    result = manager.ensure_group("g1")

    assert result["group"] == "g1"
    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["nomic-embed"]["ok"] is True
    assert by_name["nomic-embed"]["port"] == 1
    assert by_name["model-b"]["ok"] is False
    assert "pinned" in by_name["model-b"]["detail"]
    assert by_name["model-z"]["ok"] is False


def test_ensure_group_unknown_group_raises_not_found():
    manager, adapter = make_manager()

    with pytest.raises(NotFoundError):
        manager.ensure_group("no-such-group")


def test_ensure_group_with_allowed_models_reports_others_as_forbidden():
    manager, adapter = make_manager()
    manager.registry.groups["g1"] = ["nomic-embed", "model-b"]

    result = manager.ensure_group("g1", allowed_models={"nomic-embed"})

    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["nomic-embed"]["ok"] is True
    assert adapter.start_calls == ["nomic-embed"]  # model-b was never even attempted
    assert by_name["model-b"]["ok"] is False
    assert "not permitted" in by_name["model-b"]["detail"]


def test_verify_group_best_effort(monkeypatch):
    manager, adapter = make_manager()
    # Give the failing member its own gpu_group so its verify() failure comes
    # from the (mocked) request itself, not a gpu_group conflict with model-b.
    manager.registry.gpu_groups["gpu1"] = GpuGroupConfig(system="sleek")
    manager.registry.models["model-down"] = ModelDefinition(
        idle_timeout_seconds=60,
        candidates=[Candidate(engine="eng", gpu_group="gpu1", port=5, model_path="/m/down.gguf")],
    )
    manager.registry.groups["g1"] = ["model-b", "model-down"]

    def fake_post(url, json, timeout):
        if ":2/" in url:
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=httpx.Request("POST", url))
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = manager.verify_group("g1")

    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["model-b"]["ok"] is True
    assert by_name["model-down"]["ok"] is False
    assert "connection refused" in by_name["model-down"]["detail"]


def test_stop_group_best_effort():
    manager, adapter = make_manager()
    manager.registry.groups["g1"] = ["model-b", "model-z"]
    manager.ensure("model-b")

    result = manager.stop_group("g1")

    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["model-b"]["ok"] is True
    assert by_name["model-z"]["ok"] is True  # stop() on an already-stopped model is a no-op, not an error
    assert manager.status("model-b")["active"] is False


def test_stop_group_with_allowed_models_skips_others():
    manager, adapter = make_manager()
    manager.registry.groups["g1"] = ["model-b", "model-z"]
    manager.ensure("model-b")

    result = manager.stop_group("g1", allowed_models={"model-b"})

    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["model-b"]["ok"] is True
    assert by_name["model-z"]["ok"] is False
    assert "not permitted" in by_name["model-z"]["detail"]
    assert manager.status("model-b")["active"] is False
    assert adapter.stop_calls == ["model-b"]  # model-z was never even attempted


def test_group_system_models_groups_members_sharing_a_system():
    manager, _ = make_manager()
    # nomic-embed and model-b both only have a candidate on "sleek" in the
    # default registry - the realistic "one model per machine" case the
    # group-stats feature is for, where several members still land on the
    # same box.
    manager.registry.groups["g1"] = ["nomic-embed", "model-b"]

    result = manager.group_system_models("g1")

    assert result == {"sleek": ["nomic-embed", "model-b"]}


def test_group_system_models_with_allowed_models_drops_others_silently():
    manager, _ = make_manager()
    manager.registry.groups["g1"] = ["nomic-embed", "model-b"]

    result = manager.group_system_models("g1", allowed_models={"nomic-embed"})

    assert result == {"sleek": ["nomic-embed"]}


def test_group_system_models_splits_members_across_systems():
    manager, _ = make_manager()
    manager.registry.systems["monolith"] = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    manager.registry.engines["eng2"] = EngineConfig(system="monolith", type="llama.cpp", binary="C:\\x.exe")
    manager.registry.models["model-remote"] = ModelDefinition(
        candidates=[Candidate(engine="eng2", gpu_group="gpu0", port=9, model_path="C:\\m.gguf")]
    )
    manager.registry.groups["g1"] = ["model-b", "model-remote"]

    result = manager.group_system_models("g1")

    assert result == {"sleek": ["model-b"], "monolith": ["model-remote"]}


def test_group_system_models_uses_active_candidate_when_running():
    manager, _ = make_manager()
    manager.registry.systems["monolith"] = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    manager.registry.engines["eng2"] = EngineConfig(system="monolith", type="llama.cpp", binary="C:\\x.exe")
    manager.registry.models["model-either"] = ModelDefinition(
        candidates=[
            Candidate(engine="eng", gpu_group="gpu0", port=10, model_path="/m/either.gguf"),
            Candidate(engine="eng2", gpu_group="gpu0", port=10, model_path="C:\\either.gguf"),
        ]
    )
    manager.registry.groups["g1"] = ["model-either"]
    manager.state.set_active("model-either", 1)  # the monolith candidate is the one actually running

    result = manager.group_system_models("g1")

    assert result == {"monolith": ["model-either"]}


def test_group_system_models_unknown_group_raises_not_found():
    manager, _ = make_manager()

    with pytest.raises(NotFoundError):
        manager.group_system_models("no-such-group")


def test_ensure_resolves_dependency_before_dependent():
    manager, adapter = make_manager()
    manager.registry.gpu_groups["gpu1"] = GpuGroupConfig(system="sleek")
    manager.registry.models["model-dep"] = ModelDefinition(
        candidates=[Candidate(engine="eng", gpu_group="gpu1", port=50, model_path="/m/dep.gguf")],
        depends_on=["model-b"],
    )

    manager.ensure("model-dep")

    assert adapter.start_calls == ["model-b", "model-dep"]


def test_ensure_propagates_a_failing_dependency():
    manager, adapter = make_manager()
    manager.registry.gpu_groups["gpu1"] = GpuGroupConfig(system="sleek")
    manager.registry.models["model-dep"] = ModelDefinition(
        candidates=[Candidate(engine="eng", gpu_group="gpu1", port=51, model_path="/m/dep.gguf")],
        depends_on=["does-not-exist"],
    )

    with pytest.raises(NotFoundError):
        manager.ensure("model-dep")
    assert adapter.start_calls == []


def test_concurrent_ensure_calls_for_the_same_model_only_start_it_once():
    """The race _ensure_lock exists to close: without it, several threads
    could all observe "not yet active" before any of them finishes starting
    it and would each call adapter.start()."""
    manager, adapter = make_manager()
    original_start = adapter.start

    def slow_start(definition_name, candidate):
        time.sleep(0.05)
        return original_start(definition_name, candidate)

    adapter.start = slow_start

    threads = [threading.Thread(target=manager.ensure, args=("model-z",)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert adapter.start_calls == ["model-z"]


def test_dry_run_reports_start_for_a_fresh_model_without_starting_it():
    manager, adapter = make_manager()
    result = manager.dry_run("model-z")
    assert result["action"] == "start"
    assert result["candidate_index"] == 0
    assert adapter.start_calls == []


def test_dry_run_reports_keep_for_an_already_running_model():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    result = manager.dry_run("model-b")
    assert result["action"] == "keep"
    assert adapter.start_calls == ["model-b"]  # only from ensure(), not from dry_run


def test_dry_run_reports_swap_without_actually_evicting():
    manager, adapter = make_manager()
    manager.ensure("model-b")
    manager.state.get("model-b").last_used = datetime.now(timezone.utc) - timedelta(seconds=120)

    result = manager.dry_run("model-z")

    assert result["action"] == "swap"
    assert result["evicts"] == "model-b"
    assert adapter.stop_calls == []
    assert manager.status("model-b")["active"] is True


def test_dry_run_reports_blocked_when_occupant_is_pinned():
    manager, _ = make_manager()
    manager.ensure("nomic-embed")

    result = manager.dry_run("model-z")

    assert result["action"] == "blocked"
    assert "pinned" in result["blocked_reason"]


def test_dry_run_reports_missing_model_file():
    manager, _ = make_manager()
    result = manager.dry_run("model-z")
    assert result["files_ok"] is False
    assert "/m/z.gguf" in result["missing_paths"]


def test_dry_run_recurses_into_dependencies():
    manager, _ = make_manager()
    manager.registry.gpu_groups["gpu1"] = GpuGroupConfig(system="sleek")
    manager.registry.models["model-dep"] = ModelDefinition(
        candidates=[Candidate(engine="eng", gpu_group="gpu1", port=50, model_path="/m/dep.gguf")],
        depends_on=["model-b"],
    )

    result = manager.dry_run("model-dep")

    assert len(result["depends_on"]) == 1
    assert result["depends_on"][0]["model"] == "model-b"


def test_dry_run_group_is_best_effort():
    manager, _ = make_manager()
    manager.registry.groups["g1"] = ["model-b", "model-z"]

    result = manager.dry_run_group("g1")

    names = {r["model"] for r in result["results"]}
    assert names == {"model-b", "model-z"}


def test_dry_run_group_with_allowed_models_reports_others_as_forbidden():
    manager, _ = make_manager()
    manager.registry.groups["g1"] = ["model-b", "model-z"]

    result = manager.dry_run_group("g1", allowed_models={"model-b"})

    by_name = {r["model"]: r for r in result["results"]}
    assert by_name["model-b"]["action"] != "forbidden"
    assert by_name["model-z"]["action"] == "forbidden"


def test_verify_group_with_allowed_models_reports_others_as_forbidden(monkeypatch):
    manager, _ = make_manager()
    manager.registry.groups["g1"] = ["model-b", "model-z"]

    def fake_post(url, json, timeout):
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = manager.verify_group("g1", allowed_models={"model-b"})

    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["model-b"]["ok"] is True
    assert by_name["model-z"]["ok"] is False
    assert "not permitted" in by_name["model-z"]["detail"]


def test_doctor_flags_missing_files():
    manager, _ = make_manager()
    result = manager.doctor()
    assert result["ok"] is False
    by_name = {m["model"]: m for m in result["models"]}
    assert by_name["model-b"]["files_ok"] is False
    assert result["systems"] == [{"system": "sleek", "ok": True, "detail": None}]


def test_doctor_reports_ok_when_files_actually_exist(tmp_path):
    registry = build_registry()
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n")
    model_file = tmp_path / "model.gguf"
    model_file.write_text("x")
    registry.engines["eng"].binary = str(binary)
    for model in registry.models.values():
        for candidate in model.candidates:
            candidate.model_path = str(model_file)
    manager = Manager(registry, StateStore())
    manager.adapters = FakeAdapterRegistry(FakeAdapter())

    result = manager.doctor()

    assert result["ok"] is True


def test_validate_registry_reports_errors_without_touching_live_state():
    manager, _ = make_manager()
    live_before = manager.registry.model_dump(mode="json")
    bad = {"systems": {}, "gpu_groups": {}, "engines": {}, "models": {"a": {"candidates": [{"engine": "missing", "gpu_group": "missing", "port": 1}]}}}

    result = Manager.validate_registry(bad)

    assert result["ok"] is False
    assert len(result["errors"]) >= 1
    assert manager.registry.model_dump(mode="json") == live_before


def test_validate_registry_accepts_a_valid_document():
    data = build_registry().model_dump(mode="json")
    assert Manager.validate_registry(data) == {"ok": True, "errors": []}
