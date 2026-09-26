import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmanager import access_log
from modelmanager.coordinator.app import install_access_log_middleware
from modelmanager.coordinator.manager import Manager
from modelmanager.coordinator.routes import router
from modelmanager.coordinator.state import StateStore
from tests.test_manager import build_registry


@pytest.fixture(autouse=True)
def isolate_local_state(monkeypatch, tmp_path):
    # These tests use a real Manager/LlamaCppAdapter/LocalRunner (engine "eng"
    # runs /bin/true), which otherwise writes real pid/log files under the
    # developer's actual ~/.local/state/modelmanager.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def make_client(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.manager = Manager(build_registry(), StateStore(), registry_path=tmp_path / "registry.json")
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
        json={"candidates": [{"engine": "eng", "gpu_group": "gpu0", "port": 3, "model_path": "/m/c.gguf"}]},
    )
    assert resp.status_code == 200

    listed = client.get("/v1/models/model-c")
    assert listed.status_code == 200
    assert listed.json()["name"] == "model-c"


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


def test_create_model_with_bad_reference_returns_422(tmp_path):
    client = make_client(tmp_path)

    resp = client.post(
        "/v1/models/model-c",
        json={"candidates": [{"engine": "nope", "gpu_group": "gpu0", "port": 3, "model_path": "/m/c.gguf"}]},
    )

    assert resp.status_code == 422
    assert "nope" in resp.json()["detail"]


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
    assert body["path"].endswith("logs/model-b.log")


def test_log_path_endpoint_unknown_model_returns_404(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/v1/models/does-not-exist/log-path")
    assert resp.status_code == 404


def test_config_and_reload_round_trip(tmp_path):
    client = make_client(tmp_path)

    full = client.get("/v1/config")
    assert full.status_code == 200
    assert "model-b" in full.json()["models"]

    resp = client.post("/v1/models/model-c", json={"candidates": [{"engine": "eng", "gpu_group": "gpu0", "port": 3, "model_path": "/m/c.gguf"}]})
    assert resp.status_code == 200

    reg_path = tmp_path / "registry.json"
    data = json.loads(reg_path.read_text())
    del data["models"]["model-c"]
    reg_path.write_text(json.dumps(data))

    reload_resp = client.post("/v1/config/reload")
    assert reload_resp.status_code == 200
    assert "model-c" not in client.get("/v1/config").json()["models"]


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


def test_reload_without_registry_path_returns_400():
    app = FastAPI()
    app.include_router(router)
    app.state.manager = Manager(build_registry(), StateStore())
    app.state.system_runners = {}
    client = TestClient(app)

    resp = client.post("/v1/config/reload")

    assert resp.status_code == 400
