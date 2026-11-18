import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from modelmanager.adapters.base import AdapterStatus, EngineAdapter
from modelmanager.config import Candidate, EngineConfig, GpuGroupConfig, ModelDefinition, Registry, SystemConfig
from modelmanager.coordinator.manager import AmbiguousNameError, ConflictError, GpuGroupBusyError, Manager, NotFoundError
from modelmanager.errors import ForbiddenError
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


# --- resolve_name: allowed_models scoping (cross-scope name disclosure) ---
#
# routes._require_reachable deliberately never echoes a resource's real
# system name back to a scoped grant, specifically to stop a grant mapping
# model-name -> system for things it has no access to. An ambiguous-substring
# match that lists every matching model name - regardless of scope - is the
# same leak through a different door, since "model-b" and "model-z" below
# might sit on a system this grant was never given.

def test_resolve_name_ambiguous_substring_is_filtered_to_allowed_models():
    manager, _ = make_manager()

    # Without scoping, "model-" is ambiguous between model-b and model-z.
    # Scoped to only model-b, it must resolve cleanly instead of raising -
    # and must never mention model-z anywhere.
    assert manager.resolve_name("model-", allowed_models={"model-b"}) == "model-b"


def test_resolve_name_ambiguous_substring_outside_scope_is_not_found_not_leaked():
    manager, _ = make_manager()

    # Scoped to a model that doesn't match "model-" at all: no in-scope
    # candidates survive the filter, so this must look identical to "no
    # match" - never an AmbiguousNameError naming model-b/model-z.
    with pytest.raises(NotFoundError):
        manager.resolve_name("model-", allowed_models={"nomic-embed"})


def test_resolve_name_ambiguous_substring_still_ambiguous_within_scope():
    manager, _ = make_manager()

    # Both candidates in scope: still ambiguous, but the match list must be
    # limited to what's in scope (trivially true here since scope == both
    # candidates, but exercises that filtering doesn't suppress real
    # ambiguity between two in-scope models).
    with pytest.raises(AmbiguousNameError, match="model-b.*model-z|model-z.*model-b"):
        manager.resolve_name("model-", allowed_models={"model-b", "model-z"})


def test_resolve_name_exact_match_ignores_allowed_models():
    manager, _ = make_manager()

    # Typing the full name you already know isn't a new disclosure - the
    # caller gets the same 404 "unknown model" from _require_reachable
    # right after, not a leaked list. Exact match must stay unaffected by
    # scoping.
    assert manager.resolve_name("model-z", allowed_models={"nomic-embed"}) == "model-z"



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


# --- consistent_registry_read: must actually serialize against reloads ---

def test_consistent_registry_read_blocks_while_write_lock_is_held():
    # reload_from_disk()/_upsert() mutate the registry in place under
    # _write_lock with no synchronization on the read side otherwise - a
    # multi-step read (GET /v1/config) has to share that same lock or a
    # reload landing mid-read could leave it looking at a mix of
    # before/after state. Verify consistent_registry_read() actually waits
    # for _write_lock, using Events rather than sleeps so this isn't timing-
    # flaky.
    manager, _ = make_manager()
    writer_holds_lock = threading.Event()
    release_writer = threading.Event()

    def hold_write_lock():
        with manager._write_lock:
            writer_holds_lock.set()
            release_writer.wait(timeout=5)

    writer = threading.Thread(target=hold_write_lock)
    writer.start()
    assert writer_holds_lock.wait(timeout=5)

    reader_entered = threading.Event()

    def try_consistent_read():
        with manager.consistent_registry_read():
            reader_entered.set()

    reader = threading.Thread(target=try_consistent_read)
    reader.start()
    try:
        # The write lock is still held - the reader must not have gotten in.
        assert not reader_entered.wait(timeout=0.2)
    finally:
        release_writer.set()
        writer.join(timeout=5)
    assert reader_entered.wait(timeout=5)
    reader.join(timeout=5)


def test_consistent_registry_read_releases_the_lock_even_if_the_block_raises():
    # Standard context-manager semantics, but worth pinning explicitly: a
    # future change inside a `with manager.consistent_registry_read():`
    # block (e.g. get_config starts raising NotFoundError for something)
    # must never leave _write_lock stuck held - that would hang every
    # later reload/_upsert for the life of the process.
    manager, _ = make_manager()

    with pytest.raises(ValueError):
        with manager.consistent_registry_read():
            raise ValueError("boom")

    acquired = manager._write_lock.acquire(timeout=1)
    assert acquired, "lock was still held after the context manager exited via an exception"
    manager._write_lock.release()


# --- per-candidate scoping (allowed_systems) -------------------------------
#
# is_reachable()/resolve_name's allowed_models only proves *some* candidate
# of a model is in scope (and its dependency chain is satisfiable). These
# tests cover the narrower allowed_systems check Manager itself applies
# after deciding which *specific* candidate an action/read would actually
# touch - "shared" below has one candidate on vault (never granted) and one
# on sleek (granted), and "needs-vault-dep" depends on a vault-only model.

def build_scoped_registry() -> Registry:
    return Registry(
        systems={
            "sleek": SystemConfig(os="linux", control="local"),
            "vault": SystemConfig(os="linux", control="local"),
        },
        gpu_groups={
            "gpu-sleek": GpuGroupConfig(system="sleek"),
            "gpu-vault": GpuGroupConfig(system="vault"),
        },
        engines={
            "eng-sleek": EngineConfig(system="sleek", type="llama.cpp", binary="/bin/true"),
            "eng-vault": EngineConfig(system="vault", type="llama.cpp", binary="/bin/true"),
        },
        models={
            # Candidate 0 (vault) comes first - _ensure_loaded/dry_run try
            # candidates in order, so this is the one a pre-fix build would
            # wrongly pick first.
            "shared": ModelDefinition(
                candidates=[
                    Candidate(engine="eng-vault", gpu_group="gpu-vault", port=1, model_path="/m/shared-vault.gguf"),
                    Candidate(engine="eng-sleek", gpu_group="gpu-sleek", port=2, model_path="/m/shared-sleek.gguf"),
                ],
            ),
            "dep-on-vault": ModelDefinition(
                candidates=[Candidate(engine="eng-vault", gpu_group="gpu-vault", port=3, model_path="/m/dep.gguf")],
            ),
            "needs-vault-dep": ModelDefinition(
                depends_on=["dep-on-vault"],
                candidates=[Candidate(engine="eng-sleek", gpu_group="gpu-sleek", port=4, model_path="/m/needs.gguf")],
            ),
            # Engine on sleek (granted) but gpu_group on vault (not granted) -
            # nothing in the schema requires these to agree, so this is a
            # legal (if unusual) registry. A per-candidate check that only
            # looks at the engine's system would wrongly treat this as
            # in-scope for a grant that only has sleek.
            "mismatched": ModelDefinition(
                candidates=[Candidate(engine="eng-sleek", gpu_group="gpu-vault", port=5, model_path="/m/mismatched.gguf")],
            ),
            # The mirror image of "mismatched" - engine on vault, gpu_group
            # on sleek - so it can occupy gpu-sleek (a gpu_group bob's own
            # requests are fully allowed to use) while its own candidate is
            # actually unauthorized for bob.
            "occupant-mismatched": ModelDefinition(
                candidates=[Candidate(engine="eng-vault", gpu_group="gpu-sleek", port=6, model_path="/m/occupant.gguf")],
            ),
            "wants-sleek-gpu": ModelDefinition(
                candidates=[Candidate(engine="eng-sleek", gpu_group="gpu-sleek", port=7, model_path="/m/wants.gguf")],
            ),
            # Its own candidate is fully on sleek (both engine and
            # gpu_group), but it depends on a vault-only model - so it's
            # unreachable for a grant scoped to sleek alone (is_reachable
            # fails on the dependency), even though _candidate_allowed on
            # its own candidate would say yes.
            "hidden-occupant": ModelDefinition(
                pinned=True,
                depends_on=["dep-on-vault"],
                candidates=[Candidate(engine="eng-sleek", gpu_group="gpu-sleek", port=8, model_path="/m/hidden.gguf")],
            ),
        },
        groups={"mixed": ["shared", "dep-on-vault"]},
    )


def make_scoped_manager():
    registry = build_scoped_registry()
    manager = Manager(registry, StateStore())
    fake_adapter = FakeAdapter()
    manager.adapters = FakeAdapterRegistry(fake_adapter)
    return manager, fake_adapter


def test_ensure_skips_an_unauthorized_candidate_in_favor_of_an_allowed_one():
    manager, adapter = make_scoped_manager()

    result = manager.ensure("shared", allowed_systems={"sleek"})

    assert result.port == 2  # the sleek candidate, not vault's port 1
    assert adapter.start_calls == ["shared"]


def test_ensure_raises_forbidden_when_no_candidate_is_in_scope():
    manager, adapter = make_scoped_manager()

    with pytest.raises(ForbiddenError):
        manager.ensure("shared", allowed_systems={"some-other-system"})

    assert adapter.start_calls == []


def test_ensure_raises_forbidden_for_an_already_active_out_of_scope_candidate():
    manager, adapter = make_scoped_manager()
    manager.ensure("shared")  # unrestricted (owner) - picks vault, candidate 0

    with pytest.raises(ForbiddenError):
        manager.ensure("shared", allowed_systems={"sleek"})


def test_ensure_dependency_outside_scope_raises_forbidden_and_starts_nothing():
    manager, adapter = make_scoped_manager()

    with pytest.raises(ForbiddenError):
        manager.ensure("needs-vault-dep", allowed_systems={"sleek"})

    # Fails closed before starting anything - not a partial start of the
    # top-level model while silently skipping its dependency.
    assert adapter.start_calls == []


def test_ensure_dependency_within_scope_succeeds():
    manager, adapter = make_scoped_manager()

    result = manager.ensure("needs-vault-dep", allowed_systems={"sleek", "vault"})

    assert result.port == 4
    assert adapter.start_calls == ["dep-on-vault", "needs-vault-dep"]


# --- is_reachable: visibility/actionability follows the dependency chain -

def test_is_reachable_true_for_unrestricted_owner():
    manager, _ = make_scoped_manager()
    assert manager.is_reachable("needs-vault-dep", None) is True


def test_is_reachable_false_when_a_dependency_has_no_in_scope_candidate():
    # needs-vault-dep's own candidate is on sleek, but it depends on
    # dep-on-vault, which is vault-only - it can never actually be ensured
    # for a grant scoped to sleek alone, so it must not be "reachable".
    manager, _ = make_scoped_manager()
    assert manager.is_reachable("needs-vault-dep", {"sleek"}) is False


def test_is_reachable_true_when_the_whole_dependency_chain_is_in_scope():
    manager, _ = make_scoped_manager()
    assert manager.is_reachable("needs-vault-dep", {"sleek", "vault"}) is True


def test_is_reachable_false_for_a_model_with_no_in_scope_candidate_of_its_own():
    manager, _ = make_scoped_manager()
    assert manager.is_reachable("dep-on-vault", {"sleek"}) is False


def test_is_reachable_false_for_mismatched_engine_and_gpu_group_systems():
    # "mismatched" has engine on sleek, gpu_group on vault - no single
    # system covers both, so its one candidate is never actually usable.
    manager, _ = make_scoped_manager()
    assert manager.is_reachable("mismatched", {"sleek"}) is False
    assert manager.is_reachable("mismatched", {"sleek", "vault"}) is True


def test_is_reachable_false_for_an_unknown_model():
    manager, _ = make_scoped_manager()
    assert manager.is_reachable("does-not-exist", {"sleek"}) is False


def test_stop_raises_forbidden_for_out_of_scope_active_candidate_and_does_not_stop_it():
    manager, adapter = make_scoped_manager()
    manager.ensure("shared")  # active on vault

    with pytest.raises(ForbiddenError):
        manager.stop("shared", allowed_systems={"sleek"})

    assert adapter.stop_calls == []


def test_logs_raises_forbidden_for_out_of_scope_active_candidate():
    manager, _ = make_scoped_manager()
    manager.ensure("shared")  # active on vault

    with pytest.raises(ForbiddenError):
        manager.logs("shared", allowed_systems={"sleek"})


def test_log_location_raises_forbidden_for_out_of_scope_active_candidate():
    manager, _ = make_scoped_manager()
    manager.ensure("shared")  # active on vault

    with pytest.raises(ForbiddenError):
        manager.log_location("shared", allowed_systems={"sleek"})


def test_logs_prefers_an_allowed_candidate_over_a_hardcoded_zero_when_inactive():
    # "shared" is never ensured here - inactive. Candidate 0 is on vault
    # (not allowed), candidate 1 is on sleek (allowed). A grant scoped to
    # sleek must not get a false 403 just because candidate 0 happens to
    # come first in the list.
    manager, _ = make_scoped_manager()

    manager.logs("shared", allowed_systems={"sleek"})  # must not raise


def test_log_location_prefers_an_allowed_candidate_over_a_hardcoded_zero_when_inactive():
    manager, _ = make_scoped_manager()

    result = manager.log_location("shared", allowed_systems={"sleek"})  # must not raise

    assert result["system"] == "sleek"


def test_logs_still_fails_closed_when_inactive_and_no_candidate_is_allowed():
    manager, _ = make_scoped_manager()

    with pytest.raises(ForbiddenError):
        manager.logs("shared", allowed_systems={"some-other-system"})


def test_dry_run_raises_forbidden_when_no_candidate_is_in_scope():
    manager, _ = make_scoped_manager()

    with pytest.raises(ForbiddenError):
        manager.dry_run("shared", allowed_systems={"some-other-system"})


def test_dry_run_dependency_outside_scope_raises_forbidden():
    manager, _ = make_scoped_manager()

    with pytest.raises(ForbiddenError):
        manager.dry_run("needs-vault-dep", allowed_systems={"sleek"})


def test_dry_run_picks_the_allowed_candidate_when_scoped():
    manager, _ = make_scoped_manager()

    result = manager.dry_run("shared", allowed_systems={"sleek"})

    assert result["port"] == 2
    assert result["action"] == "start"


def test_status_masks_out_of_scope_active_candidate():
    manager, _ = make_scoped_manager()
    manager.ensure("shared")  # active on vault

    entry = manager.status("shared", allowed_systems={"sleek"})

    assert entry["active"] is False
    assert entry["candidate"] is None


def test_status_shows_active_candidate_when_it_is_in_scope():
    manager, _ = make_scoped_manager()
    manager.ensure("shared")  # active on vault

    entry = manager.status("shared", allowed_systems={"vault"})

    assert entry["active"] is True
    assert entry["candidate"]["port"] == 1


def test_status_gpu_groups_are_filtered_to_allowed_systems():
    manager, _ = make_scoped_manager()

    entry = manager.status("shared", allowed_systems={"sleek"})

    assert entry["gpu_groups"] == ["gpu-sleek"]


def test_list_models_masks_out_of_scope_active_candidates():
    manager, _ = make_scoped_manager()
    manager.ensure("shared")  # active on vault

    entries = {e["name"]: e for e in manager.list_models(allowed_systems={"sleek"})}

    assert entries["shared"]["active"] is False
    assert entries["shared"]["candidate"] is None


def test_group_ensure_reports_per_member_forbidden_for_out_of_scope_candidate():
    manager, adapter = make_scoped_manager()
    manager.ensure("shared")  # active on vault, before any grant is involved

    # "shared" passes the broader allowed_models test (it has a sleek
    # candidate), but its *active* candidate is on vault - the group call
    # must report that member as a failure, not silently succeed against
    # an unauthorized system.
    result = manager.ensure_group("mixed", allowed_models={"shared"}, allowed_systems={"sleek"})

    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["shared"]["ok"] is False


def test_group_system_models_drops_a_member_whose_active_candidate_is_out_of_scope():
    manager, _ = make_scoped_manager()
    manager.ensure("shared")  # active on vault

    result = manager.group_system_models("mixed", allowed_models={"shared"}, allowed_systems={"sleek"})

    assert "vault" not in result
    assert all("shared" not in models for models in result.values())


def test_release_raises_forbidden_for_out_of_scope_active_candidate():
    # Every sibling action (ensure/stop/logs/verify) re-checks the model's
    # actual active candidate against allowed_systems - release() must too,
    # or a grant outside a model's real system can still clear someone
    # else's lease on it (defeating the "an active lease blocks eviction"
    # guarantee for a system this grant has no business touching).
    manager, _ = make_scoped_manager()
    manager.acquire("shared")  # active on vault, with a lease held

    with pytest.raises(ForbiddenError):
        manager.release("shared", allowed_systems={"sleek"})

    # Not released - the lease must still be there afterward.
    assert manager.state.get("shared").leases


def test_release_succeeds_when_active_candidate_is_in_scope():
    manager, _ = make_scoped_manager()
    manager.acquire("shared")  # active on vault

    result = manager.release("shared", allowed_systems={"vault"})

    assert result["released"] == 1


def test_release_of_an_inactive_model_is_unaffected_by_allowed_systems():
    # Nothing to protect when the model isn't running anywhere - this must
    # stay a no-op, not a spurious ForbiddenError.
    manager, _ = make_scoped_manager()

    result = manager.release("shared", allowed_systems={"some-other-system"})

    assert result == {"released": 0, "remaining_leases": 0}


# --- a candidate's engine and gpu_group can legally name different systems -

def test_candidate_allowed_requires_both_engine_and_gpu_group_system_in_scope():
    manager, _ = make_scoped_manager()
    definition = manager.registry.models["mismatched"]
    candidate = definition.candidates[0]  # engine on sleek, gpu_group on vault

    assert manager._candidate_allowed(candidate, {"sleek"}) is False
    assert manager._candidate_allowed(candidate, {"vault"}) is False
    assert manager._candidate_allowed(candidate, {"sleek", "vault"}) is True


def test_ensure_rejects_a_candidate_whose_gpu_group_system_is_out_of_scope():
    manager, adapter = make_scoped_manager()

    with pytest.raises(ForbiddenError):
        manager.ensure("mismatched", allowed_systems={"sleek"})

    assert adapter.start_calls == []


def test_ensure_succeeds_when_both_the_candidates_systems_are_allowed():
    manager, adapter = make_scoped_manager()

    result = manager.ensure("mismatched", allowed_systems={"sleek", "vault"})

    assert result.port == 5
    assert adapter.start_calls == ["mismatched"]


# --- eviction must not touch/reveal an occupant outside allowed_systems ---

def test_ensure_refuses_to_evict_an_occupant_whose_candidate_is_out_of_scope():
    manager, adapter = make_scoped_manager()
    manager.ensure("occupant-mismatched")  # owner - occupies gpu-sleek via eng-vault

    with pytest.raises(GpuGroupBusyError) as exc_info:
        manager.ensure("wants-sleek-gpu", allowed_systems={"sleek"})

    # Never actually stopped the unauthorized occupant, and never named it.
    assert "occupant-mismatched" not in adapter.stop_calls
    assert "occupant-mismatched" not in str(exc_info.value)


def test_dry_run_does_not_reveal_an_occupant_whose_candidate_is_out_of_scope():
    manager, _ = make_scoped_manager()
    manager.ensure("occupant-mismatched")  # owner - occupies gpu-sleek via eng-vault

    result = manager.dry_run("wants-sleek-gpu", allowed_systems={"sleek"})

    assert result["action"] == "blocked"
    assert result["evicts"] is None
    assert "occupant-mismatched" not in json.dumps(result, default=str)


def test_ensure_does_not_name_an_unreachable_occupant_even_with_an_in_scope_candidate():
    # "hidden-occupant"'s own candidate is fully on sleek - _candidate_allowed
    # alone would say it's fine to name. But it depends on a vault-only
    # model, so it's not is_reachable() for a grant scoped to sleek - the
    # same model GET /v1/models would hide entirely must not leak its name
    # through a gpu_group-contention message either.
    manager, adapter = make_scoped_manager()
    manager.ensure("hidden-occupant")  # owner - pinned, occupies gpu-sleek

    with pytest.raises(GpuGroupBusyError) as exc_info:
        manager.ensure("wants-sleek-gpu", allowed_systems={"sleek"})

    assert "hidden-occupant" not in str(exc_info.value)
    assert "wants-sleek-gpu" not in adapter.start_calls


def test_dry_run_does_not_name_an_unreachable_occupant_even_with_an_in_scope_candidate():
    manager, _ = make_scoped_manager()
    manager.ensure("hidden-occupant")  # owner - pinned, occupies gpu-sleek

    result = manager.dry_run("wants-sleek-gpu", allowed_systems={"sleek"})

    assert result["action"] == "blocked"
    assert "hidden-occupant" not in json.dumps(result, default=str)


# --- status() masks in_use/lease_count/last_used/idle_seconds too ---------

def test_status_masks_busy_state_for_a_hidden_active_candidate():
    manager, _ = make_scoped_manager()
    manager.acquire("shared")  # active on vault, with a lease held

    entry = manager.status("shared", allowed_systems={"sleek"})

    assert entry["active"] is False
    assert entry["in_use"] is False
    assert entry["lease_count"] == 0
    assert entry["last_used"] is None
    assert entry["idle_seconds"] is None


def test_status_shows_real_busy_state_when_active_candidate_is_in_scope():
    manager, _ = make_scoped_manager()
    manager.acquire("shared")  # active on vault

    entry = manager.status("shared", allowed_systems={"vault"})

    assert entry["active"] is True
    assert entry["in_use"] is True
    assert entry["lease_count"] == 1
    assert entry["last_used"] is not None
