from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelmanager.coordinator.manager import Manager
from modelmanager.coordinator.routes import router
from modelmanager.coordinator.state import StateStore
from tests.test_manager import build_registry


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
