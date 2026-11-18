"""Property-based tests for the scoped-grant visibility/authorization
invariants in Manager (is_reachable, _candidate_allowed, status()'s
busy-state masking).

These encode, as invariants checked against randomly generated registries,
the exact bug class a multi-round manual security review found and fixed by
hand: a model-level "has ANY candidate in an allowed system" test is not
strong enough - a grant must never see, act on, or learn the name of a model
that isn't *fully* reachable (every candidate dimension in scope, every
dependency transitively reachable too). Hand-written example-based tests
catch the specific registries someone thought to write; these fuzz the
space of valid registries (including deliberately mismatched engine/
gpu_group system pairs, which is what the real bug turned on) so the next
instance of this bug class gets caught automatically instead of needing
another manual deep-dive.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from modelmanager.config import Candidate, EngineConfig, GpuGroupConfig, ModelDefinition, Registry, SystemConfig
from modelmanager.coordinator.manager import Manager
from modelmanager.coordinator.routes import _group_visible, _require_reachable, _require_visible_group
from modelmanager.coordinator.state import StateStore
from modelmanager.errors import ModelManagerError, NotFoundError
from tests.test_manager import FakeAdapter, FakeAdapterRegistry

SYSTEM_NAMES = ["sys-a", "sys-b", "sys-c"]
MODEL_NAME_POOL = [f"model-{i}" for i in range(6)]


@st.composite
def small_registries(draw):
    """A random but always-valid Registry: 1-3 systems, a handful of
    engines/gpu_groups each independently (and possibly mismatched)
    assigned to one of those systems, and 1-5 models each with 1-3
    candidates and depends_on restricted to already-defined models (acyclic
    by construction, same guarantee Registry._cross_references enforces at
    write time - no need to fight its cycle check with a doomed draw)."""
    system_names = draw(st.lists(st.sampled_from(SYSTEM_NAMES), unique=True, min_size=1, max_size=3))
    systems = {s: SystemConfig(os="linux", control="local") for s in system_names}

    engine_names = [f"eng-{i}" for i in range(len(system_names) * 2)]
    engines = {
        en: EngineConfig(system=draw(st.sampled_from(system_names)), type="llama.cpp", binary="/bin/true")
        for en in engine_names
    }

    gpu_group_names = [f"gpu-{i}" for i in range(len(system_names) * 2)]
    gpu_groups = {gn: GpuGroupConfig(system=draw(st.sampled_from(system_names))) for gn in gpu_group_names}

    model_names = draw(st.lists(st.sampled_from(MODEL_NAME_POOL), unique=True, min_size=1, max_size=5))
    models: dict[str, ModelDefinition] = {}
    for i, mn in enumerate(model_names):
        num_candidates = draw(st.integers(min_value=1, max_value=3))
        candidates = [
            Candidate(
                engine=draw(st.sampled_from(engine_names)),
                gpu_group=draw(st.sampled_from(gpu_group_names)),
                port=1000 + i * 10 + j,
                model_path=f"/m/{mn}-{j}.gguf",
            )
            for j in range(num_candidates)
        ]
        possible_deps = model_names[:i]  # only earlier models - acyclic by construction
        depends_on = (
            draw(st.lists(st.sampled_from(possible_deps), unique=True, max_size=len(possible_deps)))
            if possible_deps
            else []
        )
        models[mn] = ModelDefinition(pinned=draw(st.booleans()), candidates=candidates, depends_on=depends_on)

    num_groups = draw(st.integers(min_value=0, max_value=2))
    groups = {
        f"group-{g}": draw(st.lists(st.sampled_from(model_names), unique=True, min_size=1, max_size=len(model_names)))
        for g in range(num_groups)
    }

    return Registry(systems=systems, gpu_groups=gpu_groups, engines=engines, models=models, groups=groups)


allowed_systems_sets = st.one_of(st.none(), st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3))

# is_reachable's recursion and the registries above are small and cheap, but
# the pytest-run (not just generation) deadline default is tighter than a
# slow CI runner sometimes needs - disabled rather than tuned, since nothing
# here does I/O and a slow example is still a real counterexample.
_settings = settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@given(registry=small_registries(), allowed=allowed_systems_sets)
@_settings
def test_is_reachable_requires_a_fully_in_scope_candidate(registry, allowed):
    manager = Manager(registry, StateStore())
    for name, definition in registry.models.items():
        reachable = manager.is_reachable(name, allowed)
        if allowed is None:
            assert reachable is True
            continue
        has_fully_allowed_candidate = any(
            registry.engines[c.engine].system in allowed and registry.gpu_groups[c.gpu_group].system in allowed
            for c in definition.candidates
        )
        if not has_fully_allowed_candidate:
            assert reachable is False, (
                f"{name!r} has no candidate with BOTH engine and gpu_group in {allowed!r}, "
                "but is_reachable said True"
            )


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3))
@_settings
def test_is_reachable_requires_every_dependency_to_be_reachable_too(registry, allowed):
    manager = Manager(registry, StateStore())
    for name, definition in registry.models.items():
        if manager.is_reachable(name, allowed):
            for dep in definition.depends_on:
                assert manager.is_reachable(dep, allowed), (
                    f"{name!r} was reachable but its dependency {dep!r} was not - "
                    f"{name!r} can never actually be ensured and must not have been reachable"
                )


@given(
    registry=small_registries(),
    allowed_small=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3),
    extra=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3),
)
@_settings
def test_is_reachable_is_monotonic_in_allowed_systems(registry, allowed_small, extra):
    # Granting MORE systems can never make a model that was reachable
    # become unreachable.
    bigger = allowed_small | extra
    manager = Manager(registry, StateStore())
    for name in registry.models:
        if manager.is_reachable(name, allowed_small):
            assert manager.is_reachable(name, bigger)


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_ensure_never_leaks_an_unreachable_models_name(registry, allowed, data):
    target = data.draw(st.sampled_from(sorted(registry.models)))
    manager = Manager(registry, StateStore())
    manager.adapters = FakeAdapterRegistry(FakeAdapter())
    unreachable = {n for n in registry.models if not manager.is_reachable(n, allowed)}

    try:
        manager.ensure(target, allowed_systems=allowed)
    except ModelManagerError as exc:
        message = str(exc)
        for hidden in unreachable:
            if hidden == target:
                continue  # the caller already knows the exact name they asked for
            assert hidden not in message, (
                f"ensure({target!r}) error leaked unreachable model {hidden!r}: {message!r}"
            )


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_dry_run_never_leaks_an_unreachable_models_name(registry, allowed, data):
    target = data.draw(st.sampled_from(sorted(registry.models)))
    manager = Manager(registry, StateStore())
    manager.adapters = FakeAdapterRegistry(FakeAdapter())
    unreachable = {n for n in registry.models if not manager.is_reachable(n, allowed)}

    try:
        result = manager.dry_run(target, allowed_systems=allowed)
    except ModelManagerError as exc:
        message = str(exc)
    else:
        import json

        message = json.dumps(result, default=str)

    for hidden in unreachable:
        if hidden == target:
            continue
        assert hidden not in message, f"dry_run({target!r}) leaked unreachable model {hidden!r}: {message!r}"


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_ensure_never_starts_an_unreachable_model(registry, allowed, data):
    target = data.draw(st.sampled_from(sorted(registry.models)))
    assume(not Manager(registry, StateStore()).is_reachable(target, allowed))

    manager = Manager(registry, StateStore())
    adapter = FakeAdapter()
    manager.adapters = FakeAdapterRegistry(adapter)

    try:
        manager.ensure(target, allowed_systems=allowed)
    except ModelManagerError:
        pass
    assert target not in adapter.start_calls, (
        f"{target!r} is not reachable under {allowed!r} but ensure() started it anyway"
    )


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_status_masks_busy_state_whenever_candidate_is_masked(registry, allowed, data):
    target = data.draw(st.sampled_from(sorted(registry.models)))
    manager = Manager(registry, StateStore())
    manager.adapters = FakeAdapterRegistry(FakeAdapter())
    try:
        manager.ensure(target)  # owner - unrestricted, so this is whatever it's going to be
    except ModelManagerError:
        pass

    entry = manager.status(target, allowed_systems=allowed)

    if entry["candidate"] is None:
        assert entry["active"] is False
        assert entry["in_use"] is False
        assert entry["lease_count"] == 0
        assert entry["last_used"] is None
        assert entry["idle_seconds"] is None


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_group_visibility_matches_all_members_being_reachable(registry, allowed, data):
    """_group_visible (the invariant behind GET /v1/groups, GET /v1/groups/
    {name}, and _require_visible_group's 404 gate in front of every group
    action/stats route) must hide a group the instant ANY member isn't
    reachable - never partially, never based on some weaker per-member
    test. This is the group-subsystem analog of
    test_is_reachable_requires_a_fully_in_scope_candidate: same invariant,
    an entirely different set of 5 routes/methods consuming it."""
    model_names = sorted(registry.models)
    members = data.draw(st.lists(st.sampled_from(model_names), unique=True, min_size=1, max_size=len(model_names)))
    manager = Manager(registry, StateStore())

    visible = _group_visible(manager, members, allowed)
    all_reachable = all(manager.is_reachable(m, allowed) for m in members)

    assert visible == all_reachable, (
        f"_group_visible disagreed with 'every member reachable' for members={members!r}, allowed={allowed!r}"
    )


def _fake_grant_request(allowed_systems: set[str] | None):
    """A minimal stand-in for the FastAPI Request _grant()/_allowed_systems()
    actually read: just enough shape (.state.grant) for those functions to
    work unmodified. allowed_systems=None models the owner token."""
    grant = (
        None
        if allowed_systems is None
        else {"name": "fuzz-grant", "allowed_systems": sorted(allowed_systems), "system_expirations": {}}
    )
    return SimpleNamespace(state=SimpleNamespace(grant=grant))


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_require_reachable_raises_not_found_never_forbidden_for_an_unreachable_model(registry, allowed, data):
    """Regression test for a real bug found by the tier-5 adversarial smoke
    test: an *exact* model name that exists but is unreachable for this
    grant used to raise ForbiddenError (403) at the route gate, while a
    substring guess for the same name - or a name that truly doesn't
    exist - 404'd. That status-code difference let a grant confirm an
    out-of-scope model's exact name exists, the same no-echo violation
    the project otherwise explicitly guards against. _require_reachable
    must raise NotFoundError, with the exact same message resolve_name
    itself uses for a genuine miss, for every unreachable exact name."""
    target = data.draw(st.sampled_from(sorted(registry.models)))
    manager = Manager(registry, StateStore())
    request = _fake_grant_request(allowed)

    if manager.is_reachable(target, allowed):
        return  # nothing to assert - this exact name is legitimately visible

    with pytest.raises(NotFoundError) as exc_info:
        _require_reachable(request, manager, target)
    assert str(exc_info.value) == f"unknown model {target!r}"


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_require_visible_group_matches_group_visible_on_a_real_registry(registry, allowed, data):
    """small_registries() now also populates registry.groups, so this
    exercises _require_visible_group's *actual* registry.groups[name]
    lookup (the real route-level gate in front of every group action/stats
    route) end to end, instead of only the lower-level _group_visible
    helper with members drawn independently of any real group."""
    assume(registry.groups)
    group_name = data.draw(st.sampled_from(sorted(registry.groups)))
    manager = Manager(registry, StateStore())
    request = _fake_grant_request(allowed)
    members = registry.groups[group_name]

    expected_visible = all(manager.is_reachable(m, allowed) for m in members)

    if expected_visible:
        result = _require_visible_group(request, manager, group_name)
        assert sorted(result) == sorted(members)
    else:
        with pytest.raises(NotFoundError):
            _require_visible_group(request, manager, group_name)


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_a_successful_action_always_touches_a_fully_in_scope_candidate(registry, allowed, data):
    """The positive mirror of test_ensure_never_starts_an_unreachable_model:
    not just "never starts something unreachable", but whenever ensure()/
    acquire() DOES succeed for a scoped grant, the specific candidate it
    actually selected and started has both its engine's and its
    gpu_group's system inside allowed_systems - the root property the
    whole per-candidate redesign exists to guarantee (verify() delegates
    its own candidate selection straight to ensure(), so it inherits this
    for free rather than needing its own check)."""
    target = data.draw(st.sampled_from(sorted(registry.models)))

    def candidate_in_scope(candidate_index: int) -> bool:
        candidate = registry.models[target].candidates[candidate_index]
        return (
            registry.engines[candidate.engine].system in allowed
            and registry.gpu_groups[candidate.gpu_group].system in allowed
        )

    ensure_manager = Manager(registry, StateStore())
    ensure_manager.adapters = FakeAdapterRegistry(FakeAdapter())
    try:
        ensure_result = ensure_manager.ensure(target, allowed_systems=allowed)
    except ModelManagerError:
        pass
    else:
        assert candidate_in_scope(ensure_result.candidate_index), (
            f"ensure({target!r}) succeeded under {allowed!r} via candidate "
            f"{ensure_result.candidate_index}, which isn't fully in scope"
        )

    acquire_manager = Manager(registry, StateStore())
    acquire_manager.adapters = FakeAdapterRegistry(FakeAdapter())
    try:
        acquire_result = acquire_manager.acquire(target, allowed_systems=allowed)
    except ModelManagerError:
        pass
    else:
        assert candidate_in_scope(acquire_result.candidate_index), (
            f"acquire({target!r}) succeeded under {allowed!r} via candidate "
            f"{acquire_result.candidate_index}, which isn't fully in scope"
        )


# --- threat model: a friend's box (holding a grant token) is compromised --
#
# Every test above checks one call at a time. A real compromise doesn't make
# one clean request and stop - whoever has the stolen/replayed token probes
# everything reachable, in whatever order, trying to learn or touch
# something outside what that grant was ever given. This drives a random
# SEQUENCE of every action a grant can take, against random (including
# nonexistent) model names, and checks after every single step - not just
# once at the end - that nothing outside allowed_systems was ever revealed
# or touched. The grant's token/allowed_systems never change mid-sequence
# here (that's the *other* half of this project's threat model - a
# revoked/expired grant - already covered by tests/test_grants.py and
# test_app_startup.py's expiry/revoke-all tests); this test is specifically
# about what a live, legitimately-scoped-but-malicious holder can do with
# everything it's already allowed to call, for as long as it's willing to
# keep calling it.

ACTIONS = ["ensure", "acquire", "release", "dry_run", "stop", "logs", "log_location", "verify"]


def _run_action(manager: Manager, action: str, target: str, allowed: set[str]) -> str:
    """Runs one action, returns a string safe to grep for a leaked name -
    the actual response/state on success, or the exception message on a
    ModelManagerError (anything else is a real bug and is let through to
    fail the test loudly, not swallowed as if it were an access decision)."""
    if action == "ensure":
        result = manager.ensure(target, allowed_systems=allowed)
        return json.dumps({"host": result.host, "port": result.port})
    if action == "acquire":
        result = manager.acquire(target, allowed_systems=allowed)
        return json.dumps({"host": result.host, "port": result.port})
    if action == "release":
        return json.dumps(manager.release(target, allowed_systems=allowed))
    if action == "dry_run":
        return json.dumps(manager.dry_run(target, allowed_systems=allowed), default=str)
    if action == "stop":
        manager.stop(target, allowed_systems=allowed)
        return ""
    if action == "logs":
        return manager.logs(target, allowed_systems=allowed)
    if action == "log_location":
        return json.dumps(manager.log_location(target, allowed_systems=allowed))
    if action == "verify":
        return json.dumps(manager.verify(target, timeout=0.01, allowed_systems=allowed))
    raise AssertionError(f"unhandled action {action!r}")


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@settings(
    max_examples=500, deadline=None,
    # function_scoped_fixture: `monkeypatch` below is applied identically
    # (and harmlessly) on every generated example - it isn't being used to
    # isolate state between them, so the usual reset-per-example concern
    # doesn't apply here.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_a_compromised_peer_cannot_escalate_through_any_sequence_of_actions(registry, allowed, data, monkeypatch):
    # No real server is listening on these fake ports - force verify()'s
    # outbound call to fail fast and deterministically instead of making a
    # real (and in a sandboxed test environment, unpredictable) network
    # connection attempt.
    monkeypatch.setattr("httpx.post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no server in this test")))

    manager = Manager(registry, StateStore())
    manager.adapters = FakeAdapterRegistry(FakeAdapter())
    unreachable = {n for n in registry.models if not manager.is_reachable(n, allowed)}
    probe_pool = sorted(registry.models) + ["totally-made-up-model-name"]

    def candidate_in_scope(model_name: str, candidate_index: int) -> bool:
        candidate = registry.models[model_name].candidates[candidate_index]
        return (
            registry.engines[candidate.engine].system in allowed
            and registry.gpu_groups[candidate.gpu_group].system in allowed
        )

    num_steps = data.draw(st.integers(min_value=1, max_value=12))
    for step in range(num_steps):
        action = data.draw(st.sampled_from(ACTIONS))
        target = data.draw(st.sampled_from(probe_pool))

        try:
            message = _run_action(manager, action, target, allowed)
        except ModelManagerError as exc:
            message = str(exc)

        for hidden in unreachable:
            if hidden == target:
                continue  # the caller already knows the exact name it probed with
            assert hidden not in message, (
                f"step {step} ({action}({target!r})) leaked unreachable model {hidden!r}: {message!r}"
            )

        if action in ("ensure", "acquire") and target in registry.models:
            state = manager.state.get(target)
            if state.is_active:
                assert candidate_in_scope(target, state.active_candidate_index), (
                    f"step {step} ({action}({target!r})) left it active on an out-of-scope candidate"
                )


@given(registry=small_registries(), allowed=st.sets(st.sampled_from(SYSTEM_NAMES), max_size=3), data=st.data())
@_settings
def test_logs_and_log_location_never_false_reject_an_inactive_model_with_any_in_scope_candidate(registry, allowed, data):
    """Regression test for a real bug found reviewing this exact fix: logs()/
    log_location() picked a hardcoded candidate 0 whenever a model was
    inactive, then checked THAT candidate's scope - so a grant with access
    via only a later candidate (e.g. the same model available on two
    systems) got a false 403 on an inactive multi-candidate model it
    legitimately has some access to. The fix prefers the first candidate
    that's actually in scope when inactive; this fuzzes that it holds for
    every shape of candidate list, not just the one example registry that
    was hand-written to catch it."""
    target = data.draw(st.sampled_from(sorted(registry.models)))
    definition = registry.models[target]
    manager = Manager(registry, StateStore())
    manager.adapters = FakeAdapterRegistry(FakeAdapter())
    # Never ensured - the model stays inactive, which is exactly the path
    # that picked the scope-blind hardcoded 0.
    assert not manager.state.get(target).is_active

    has_an_allowed_candidate = any(
        registry.engines[c.engine].system in allowed and registry.gpu_groups[c.gpu_group].system in allowed
        for c in definition.candidates
    )

    try:
        manager.logs(target, allowed_systems=allowed)
    except ModelManagerError:
        assert not has_an_allowed_candidate, (
            f"logs({target!r}) was rejected under {allowed!r} even though it has an in-scope candidate"
        )

    try:
        manager.log_location(target, allowed_systems=allowed)
    except ModelManagerError:
        assert not has_an_allowed_candidate, (
            f"log_location({target!r}) was rejected under {allowed!r} even though it has an in-scope candidate"
        )
