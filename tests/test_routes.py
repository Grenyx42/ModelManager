import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmanager import access_log, lifecycle_log
from modelmanager.config import Candidate, EngineConfig, ModelDefinition, SystemConfig
from modelmanager.coordinator import routes as routes_module
from modelmanager.errors import CommandFailedError, HostUnreachableError
from modelmanager.coordinator.app import install_access_log_middleware, install_error_handler
from modelmanager.coordinator.manager import Manager
from modelmanager.coordinator.routes import router
from modelmanager.coordinator.state import StateStore
from tests.test_manager import build_registry


@pytest.fixture(autouse=True)
def isolate_local_state(monkeypatch, tmp_path):
    # These tests use a real Manager/LlamaCppAdapter/LocalRunner, which
    # otherwise writes real pid/log files under the developer's actual
    # ~/.local/state/modelmanager.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def _fake_lingering_binary(tmp_path):
    # start() now checks the process is still running after a brief grace
    # period (see STARTUP_GRACE_SECONDS in llamacpp.py) - unlike /bin/true,
    # this actually stays up long enough for that check to see it as such.
    script = tmp_path / "fake-llama-server"
    script.write_text("#!/bin/sh\nsleep 100\n")
    script.chmod(0o755)
    return str(script)


def make_client(tmp_path):
    app = FastAPI()
    app.include_router(router)
    install_error_handler(app)
    registry = build_registry()
    registry.engines["eng"].binary = _fake_lingering_binary(tmp_path)
    app.state.manager = Manager(registry, StateStore(), registry_path=tmp_path / "registry.json")
    app.state.system_runners = {}
    return TestClient(app)


def test_help_endpoint_documents_model_schema(tmp_path):
    client = make_client(tmp_path)

    resp = client.get("/v1/help")

    assert resp.status_code == 200
    body = resp.json()
    assert "model" in body["schema"]
    assert "candidates" in body["schema"]["model"]["fields"]
    assert body["schema"]["model"]["fields"]["candidates"]["required"] is True
    assert any(e["path"] == "/v1/models/{name}" and e["method"] == "POST" for e in body["endpoints"])


def test_create_model_via_rest_round_trip(tmp_path):
    client = make_client(tmp_path)

    resp = client.post(
        "/v1/models/model-c",
        json={"candidates": [{"engine": "eng", "gpu_group": "gpu0", "port": 4, "model_path": "/m/c.gguf"}]},
    )
    assert resp.status_code == 200

    listed = client.get("/v1/models/model-c")
    assert listed.status_code == 200
    assert listed.json()["name"] == "model-c"


def test_ensure_resolves_a_unique_partial_name(tmp_path):
    client = make_client(tmp_path)

    resp = client.post("/v1/models/nomic/ensure")

    assert resp.status_code == 200
    assert client.get("/v1/models/nomic-embed").json()["active"] is True


def test_ensure_ambiguous_partial_name_returns_409(tmp_path):
    client = make_client(tmp_path)

    resp = client.post("/v1/models/model-/ensure")

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "AMBIGUOUS_NAME"
    assert "model-b" in body["error"]
    assert "model-z" in body["error"]


def test_ensure_returns_502_when_process_crashes_on_startup(tmp_path, monkeypatch):
    # Needs a real (if tiny) grace period - unlike every other test here,
    # this one relies on the child process having actually exited by the
    # time ensure() checks it, which a patched-to-0 sleep can't guarantee.
    monkeypatch.setattr("modelmanager.adapters.llamacpp.STARTUP_GRACE_SECONDS", 0.2)
    client = make_client(tmp_path)
    manager = client.app.state.manager
    crashing_binary = tmp_path / "fake-llama-server-crash"
    crashing_binary.write_text('#!/bin/sh\necho "bad --model path" >&2\nexit 1\n')
    crashing_binary.chmod(0o755)
    manager.registry.engines["eng"].binary = str(crashing_binary)

    resp = client.post("/v1/models/model-b/ensure")

    assert resp.status_code == 502
    body = resp.json()
    assert body["code"] == "ENGINE_CONTROL_FAILED"
    assert "bad --model path" in body["error"]
    assert client.get("/v1/models/model-b").json()["active"] is False


def test_access_log_middleware_records_requests(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.manager = Manager(build_registry(), StateStore(), registry_path=tmp_path / "registry.json")
    app.state.system_runners = {}
    access_log.configure()
    install_access_log_middleware(app)
    client = TestClient(app)

    resp = client.get("/v1/models/model-b")
    assert resp.status_code == 200

    log_text = access_log.tail()
    assert "GET /v1/models/model-b -> 200" in log_text
    assert "ms)" in log_text


def test_access_log_endpoint_returns_tail(tmp_path):
    client = make_client(tmp_path)
    access_log.configure()
    access_log.record("1.2.3.4:5", "GET", "/v1/models", 200, 1.0)

    resp = client.get("/v1/access-log", params={"lines": 10})

    assert resp.status_code == 200
    assert "1.2.3.4:5 GET /v1/models -> 200" in resp.json()["log"]


def test_lifecycle_log_endpoint_returns_tail(tmp_path):
    client = make_client(tmp_path)
    lifecycle_log.configure()
    lifecycle_log.record("model-b: crash detected (process no longer running) - restarting")

    resp = client.get("/v1/lifecycle-log", params={"lines": 10})

    assert resp.status_code == 200
    assert "model-b: crash detected" in resp.json()["log"]


def test_create_model_with_bad_reference_returns_422(tmp_path):
    client = make_client(tmp_path)

    resp = client.post(
        "/v1/models/model-c",
        json={"candidates": [{"engine": "nope", "gpu_group": "gpu0", "port": 3, "model_path": "/m/c.gguf"}]},
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "nope" in body["error"]


def test_get_unknown_model_returns_404(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/v1/models/does-not-exist")
    assert resp.status_code == 404


def test_log_path_endpoint_reports_native_path(tmp_path):
    client = make_client(tmp_path)

    resp = client.get("/v1/models/model-b/log-path")

    assert resp.status_code == 200
    body = resp.json()
    assert body["system"] == "sleek"
    assert body["control"] == "local"
    assert body["os"] == "linux"
    assert body["ssh_host"] is None
    assert len(body["paths"]) == 1
    assert body["paths"][0].endswith("logs/model-b.log")


def test_log_path_endpoint_unknown_model_returns_404(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/v1/models/does-not-exist/log-path")
    assert resp.status_code == 404


def test_config_and_reload_round_trip(tmp_path):
    client = make_client(tmp_path)

    full = client.get("/v1/config")
    assert full.status_code == 200
    assert "model-b" in full.json()["models"]

    resp = client.post("/v1/models/model-c", json={"candidates": [{"engine": "eng", "gpu_group": "gpu0", "port": 4, "model_path": "/m/c.gguf"}]})
    assert resp.status_code == 200

    reg_path = tmp_path / "registry.json"
    data = json.loads(reg_path.read_text())
    del data["models"]["model-c"]
    reg_path.write_text(json.dumps(data))

    reload_resp = client.post("/v1/config/reload")
    assert reload_resp.status_code == 200
    assert "model-c" not in client.get("/v1/config").json()["models"]

    status = client.get("/v1/config/reload-status")
    assert status.status_code == 200
    assert status.json()["reload_count"] == 1
    assert status.json()["last_reload_at"] is not None


def test_acquire_release_and_forced_stop(tmp_path):
    client = make_client(tmp_path)

    acquired = client.post("/v1/models/model-b/acquire")
    assert acquired.status_code == 200
    body = acquired.json()
    assert body["lease_id"]
    assert client.get("/v1/models/model-b").json()["in_use"] is True

    blocked_stop = client.post("/v1/models/model-b/stop")
    assert blocked_stop.status_code == 409

    forced_stop = client.post("/v1/models/model-b/stop", params={"force": "true"})
    assert forced_stop.status_code == 200
    assert client.get("/v1/models/model-b").json()["active"] is False


def test_release_endpoint_frees_lease(tmp_path):
    client = make_client(tmp_path)
    acquired = client.post("/v1/models/model-b/acquire").json()

    released = client.post("/v1/models/model-b/release", params={"lease_id": acquired["lease_id"]})

    assert released.status_code == 200
    assert released.json() == {"released": 1, "remaining_leases": 0}
    assert client.get("/v1/models/model-b").json()["in_use"] is False


def test_create_group_via_rest_round_trip(tmp_path):
    client = make_client(tmp_path)

    created = client.post("/v1/groups/g1", json=["nomic-embed", "model-b"])
    assert created.status_code == 200
    assert created.json() == {"name": "g1", "models": ["nomic-embed", "model-b"]}

    listed = client.get("/v1/groups/g1")
    assert listed.status_code == 200
    assert listed.json() == {"name": "g1", "models": ["nomic-embed", "model-b"]}
    assert client.get("/v1/groups").json()["g1"] == ["nomic-embed", "model-b"]


def test_create_group_with_unknown_model_returns_422(tmp_path):
    client = make_client(tmp_path)

    resp = client.post("/v1/groups/g1", json=["no-such-model"])

    assert resp.status_code == 422


def test_group_ensure_is_best_effort(tmp_path):
    client = make_client(tmp_path)
    # nomic-embed and model-b share gpu0 - ensuring both in one group means
    # the second one necessarily loses the slot, which is exactly the
    # "one member's failure doesn't block the rest" case this is for.
    client.post("/v1/groups/g1", json=["nomic-embed", "model-b"])

    resp = client.post("/v1/groups/g1/ensure")

    assert resp.status_code == 200
    by_name = {r["name"]: r for r in resp.json()["results"]}
    assert by_name["nomic-embed"]["ok"] is True
    assert by_name["model-b"]["ok"] is False


def test_group_ensure_unknown_group_returns_404(tmp_path):
    client = make_client(tmp_path)

    resp = client.post("/v1/groups/no-such-group/ensure")

    assert resp.status_code == 404


def test_group_stop_is_best_effort(tmp_path):
    client = make_client(tmp_path)
    client.post("/v1/groups/g1", json=["nomic-embed", "model-b"])
    client.post("/v1/models/nomic-embed/ensure")

    resp = client.post("/v1/groups/g1/stop")

    assert resp.status_code == 200
    by_name = {r["name"]: r for r in resp.json()["results"]}
    assert by_name["nomic-embed"]["ok"] is True
    assert by_name["model-b"]["ok"] is True  # already-stopped model-b is a no-op, not a failure
    assert client.get("/v1/models/nomic-embed").json()["active"] is False


def test_group_stats_best_effort_across_systems(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    manager = client.app.state.manager
    manager.registry.systems["monolith"] = SystemConfig(os="windows", control="ssh", ssh_host="monolith")
    manager.registry.engines["eng2"] = EngineConfig(system="monolith", type="llama.cpp", binary="C:\\x.exe")
    manager.registry.models["model-remote"] = ModelDefinition(
        candidates=[Candidate(engine="eng2", gpu_group="gpu0", port=9, model_path="C:\\m.gguf")]
    )
    manager.registry.groups["g1"] = ["model-b", "model-remote"]

    # Tag each fake runner with the system it was built for, so the fake
    # gpu_stats() below can make "monolith" fail without a real SSH call -
    # the point of this test is the per-system best-effort merge in the
    # route, not gpu_stats()/Runner internals (those are covered elsewhere).
    monkeypatch.setattr(routes_module, "build_runner", lambda system: system.ssh_host or "sleek")

    def fake_gpu_stats(runner, timeout=15):
        if runner == "monolith":
            raise RuntimeError("ssh unreachable")
        return [{"index": 0, "name": "fake-gpu", "memory_used_mb": 1, "memory_total_mb": 2, "utilization_pct": 3, "temperature_c": 4}]

    monkeypatch.setattr(routes_module.gpu_stats, "gpu_stats", fake_gpu_stats)

    resp = client.get("/v1/groups/g1/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["group"] == "g1"
    by_system = {r["system"]: r for r in body["results"]}
    assert by_system["sleek"]["ok"] is True
    assert by_system["sleek"]["models"] == ["model-b"]
    assert by_system["sleek"]["gpus"][0]["name"] == "fake-gpu"
    assert by_system["monolith"]["ok"] is False
    assert by_system["monolith"]["models"] == ["model-remote"]
    assert "ssh unreachable" in by_system["monolith"]["detail"]


def test_all_systems_stats_is_best_effort_across_systems(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    manager = client.app.state.manager
    manager.registry.systems["monolith"] = SystemConfig(os="windows", control="ssh", ssh_host="monolith")

    monkeypatch.setattr(routes_module, "build_runner", lambda system: system.ssh_host or "sleek")

    def fake_gpu_stats(runner, timeout=15):
        if runner == "monolith":
            raise RuntimeError("ssh unreachable")
        return [{"index": 0, "name": "fake-gpu", "memory_used_mb": 1, "memory_total_mb": 2, "utilization_pct": 3, "temperature_c": 4}]

    monkeypatch.setattr(routes_module.gpu_stats, "gpu_stats", fake_gpu_stats)

    resp = client.get("/v1/systems/stats")

    assert resp.status_code == 200
    by_system = {r["system"]: r for r in resp.json()["results"]}
    assert by_system["sleek"]["ok"] is True
    assert by_system["sleek"]["gpus"][0]["name"] == "fake-gpu"
    assert by_system["monolith"]["ok"] is False
    assert "ssh unreachable" in by_system["monolith"]["detail"]


def test_group_stats_unknown_group_returns_404(tmp_path):
    client = make_client(tmp_path)

    resp = client.get("/v1/groups/no-such-group/stats")

    assert resp.status_code == 404


def test_system_stats_surfaces_command_failure_cleanly(tmp_path, monkeypatch):
    """A single system's stats failure (unlike the group version) isn't
    aggregated into a best-effort list - it should still come back as the
    same {error, code} shape every other error uses, not a raw {detail}."""
    client = make_client(tmp_path)

    def fake_gpu_stats(runner, timeout=15):
        raise CommandFailedError("nvidia-smi failed: command not found")

    monkeypatch.setattr(routes_module.gpu_stats, "gpu_stats", fake_gpu_stats)

    resp = client.get("/v1/systems/sleek/stats")

    assert resp.status_code == 502
    assert resp.json() == {"error": "nvidia-smi failed: command not found", "code": "COMMAND_FAILED"}


def test_deploy_agent_surfaces_ssh_failure_cleanly(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    client.app.state.manager.registry.systems["monolith"] = SystemConfig(os="windows", control="ssh", ssh_host="monolith")

    def fake_deploy(name, system, force=False):
        raise HostUnreachableError(f"cannot reach {system.ssh_host!r} over ssh: Connection refused")

    monkeypatch.setattr(routes_module.agent_deploy, "deploy_if_needed", fake_deploy)

    resp = client.post("/v1/systems/monolith/deploy-agent")

    assert resp.status_code == 502
    assert resp.json()["code"] == "HOST_UNREACHABLE"
    assert "monolith" in resp.json()["error"]


def test_reload_without_registry_path_returns_400():
    app = FastAPI()
    app.include_router(router)
    install_error_handler(app)
    app.state.manager = Manager(build_registry(), StateStore())
    app.state.system_runners = {}
    client = TestClient(app)

    resp = client.post("/v1/config/reload")

    assert resp.status_code == 400
    assert resp.json()["code"] == "RELOAD_UNAVAILABLE"


def test_version_endpoint(tmp_path):
    client = make_client(tmp_path)

    resp = client.get("/v1/version")

    assert resp.status_code == 200
    assert resp.json() == {"version": "1.0"}


def test_validate_config_endpoint_accepts_a_valid_document(tmp_path):
    client = make_client(tmp_path)

    resp = client.post("/v1/config/validate", json=build_registry().model_dump(mode="json"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "errors": []}


def test_validate_config_endpoint_reports_errors_without_changing_live_state(tmp_path):
    client = make_client(tmp_path)
    live_before = client.get("/v1/config").json()
    bad_document = {
        "systems": {},
        "gpu_groups": {},
        "engines": {},
        "models": {"a": {"candidates": [{"engine": "missing", "gpu_group": "missing", "port": 1}]}},
    }

    resp = client.post("/v1/config/validate", json=bad_document)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert len(body["errors"]) >= 1
    assert client.get("/v1/config").json() == live_before


def test_dry_run_model_endpoint(tmp_path):
    client = make_client(tmp_path)

    resp = client.get("/v1/models/model-b/dry-run")

    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "model-b"
    assert body["action"] == "start"


def test_dry_run_group_endpoint(tmp_path):
    client = make_client(tmp_path)
    client.app.state.manager.registry.groups["g1"] = ["model-b", "model-z"]

    resp = client.get("/v1/groups/g1/dry-run")

    assert resp.status_code == 200
    names = {r["model"] for r in resp.json()["results"]}
    assert names == {"model-b", "model-z"}


def test_doctor_endpoint(tmp_path):
    client = make_client(tmp_path)

    resp = client.get("/v1/doctor")

    assert resp.status_code == 200
    body = resp.json()
    assert body["systems"] == [{"system": "sleek", "ok": True, "detail": None}]
    # model-b/model-z's model_path ("/m/*.gguf") isn't a real file - doctor
    # should surface that rather than claiming everything's launchable.
    assert body["ok"] is False
