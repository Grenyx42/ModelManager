import json

import pytest
from fastapi.testclient import TestClient

from modelmanager.coordinator.app import load_registry_or_empty


def test_load_registry_or_empty_returns_parsed_registry_on_success(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"systems": {}, "gpu_groups": {}, "engines": {}, "models": {}}))

    registry, error = load_registry_or_empty(path)

    assert error is None
    assert registry.models == {}


def test_load_registry_or_empty_falls_back_on_bad_json(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text('{"systems": {}, "gpu_groups": {}, "engines": {}, "models": {}')  # missing closing brace

    registry, error = load_registry_or_empty(path)

    assert error is not None
    assert registry.systems == {}
    assert registry.models == {}


def test_load_registry_or_empty_falls_back_on_validation_error(tmp_path):
    path = tmp_path / "registry.json"
    # "models" references an engine that doesn't exist - fails Registry's cross-reference validator.
    path.write_text(json.dumps({
        "systems": {}, "gpu_groups": {}, "engines": {},
        "models": {"m": {"candidates": [{"engine": "missing", "gpu_group": "g", "port": 1, "model_ref": "x"}]}},
    }))

    registry, error = load_registry_or_empty(path)

    assert error is not None
    assert "missing" in error
    assert registry.models == {}


def test_load_registry_or_empty_falls_back_on_missing_file(tmp_path):
    registry, error = load_registry_or_empty(tmp_path / "does-not-exist.json")

    assert error is not None
    assert registry.systems == {}


def test_load_registry_or_empty_falls_back_to_last_good_backup_when_present(tmp_path):
    path = tmp_path / "registry.json"
    backup_path = tmp_path / "registry.json.last-good"
    good = {
        "systems": {"sleek": {"os": "linux", "control": "local"}},
        "gpu_groups": {}, "engines": {}, "models": {},
    }
    backup_path.write_text(json.dumps(good))
    path.write_text("{not valid json")

    registry, error = load_registry_or_empty(path)

    assert error is not None
    assert "last-good" in error
    assert "sleek" in registry.systems


def test_load_registry_or_empty_ignores_a_broken_backup_too(tmp_path):
    path = tmp_path / "registry.json"
    backup_path = tmp_path / "registry.json.last-good"
    backup_path.write_text("{also not valid json")
    path.write_text("{not valid json")

    registry, error = load_registry_or_empty(path)

    assert error is not None
    assert registry.systems == {}


def test_coordinator_stays_up_and_reports_error_when_registry_is_broken(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text("{not valid json")
    monkeypatch.setenv("MODELMANAGER_REGISTRY", str(path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("MODELMANAGER_TOKEN", "test-token")

    from modelmanager.coordinator.app import create_app

    app = create_app()
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        resp = client.get("/v1/config/reload-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["last_reload_error"] is not None

        full = client.get("/v1/config")
        assert full.status_code == 200
        assert full.json()["models"] == {}


def test_coordinator_recovers_from_backup_after_live_file_is_corrupted(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "systems": {"sleek": {"os": "linux", "control": "local"}},
        "gpu_groups": {}, "engines": {}, "models": {},
    }))
    monkeypatch.setenv("MODELMANAGER_REGISTRY", str(path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("MODELMANAGER_TOKEN", "test-token")
    auth_headers = {"Authorization": "Bearer test-token"}

    from modelmanager.coordinator.app import create_app

    with TestClient(create_app(), headers=auth_headers) as client:
        resp = client.post("/v1/gpu_groups/gpu0", json={"system": "sleek"})
        assert resp.status_code == 200

    assert (tmp_path / "registry.json.last-good").exists()

    # A bad hand edit while the coordinator is down for a restart.
    path.write_text("{not valid json")

    with TestClient(create_app(), headers=auth_headers) as client:
        status = client.get("/v1/config/reload-status").json()
        assert "last-good" in status["last_reload_error"]

        full = client.get("/v1/config").json()
        assert "gpu0" in full["gpu_groups"]  # recovered from backup, not left empty

        # Fix the file - the automatic RegistryWatcher should pick it up and
        # clear the error without needing a restart.
        path.write_text(json.dumps({"systems": {}, "gpu_groups": {}, "engines": {}, "models": {}}))
        reload_resp = client.post("/v1/config/reload")
        assert reload_resp.status_code == 200
        assert client.get("/v1/config/reload-status").json()["last_reload_error"] is None


def _minimal_app_env(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"systems": {}, "gpu_groups": {}, "engines": {}, "models": {}}))
    monkeypatch.setenv("MODELMANAGER_REGISTRY", str(registry_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    # Isolates default_token_path() from a real token that might exist at
    # the developer's actual ~/.config/modelmanager/token.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("MODELMANAGER_TOKEN", raising=False)
    monkeypatch.delenv("MODELMANAGER_TOKEN_PATH", raising=False)


def test_no_token_configured_fails_closed_on_every_route(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)

    from modelmanager.coordinator.app import create_app

    with TestClient(create_app()) as client:
        # /v1/help is otherwise the one "just tell me what this API looks
        # like" route - still refused, same as anything else, by design.
        resp = client.get("/v1/help")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "AUTH_NOT_CONFIGURED"
    assert "modelmanager auth rotate" in body["error"]


def test_missing_authorization_header_is_rejected(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "the-real-token")

    from modelmanager.coordinator.app import create_app

    with TestClient(create_app()) as client:
        resp = client.get("/v1/help")

    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


def test_wrong_token_is_rejected(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "the-real-token")

    from modelmanager.coordinator.app import create_app

    with TestClient(create_app(), headers={"Authorization": "Bearer wrong-token"}) as client:
        resp = client.get("/v1/help")

    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


def test_correct_token_is_accepted(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "the-real-token")

    from modelmanager.coordinator.app import create_app

    with TestClient(create_app(), headers={"Authorization": "Bearer the-real-token"}) as client:
        resp = client.get("/v1/help")

    assert resp.status_code == 200


def test_rotated_token_file_takes_effect_without_restart(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    from modelmanager import auth
    from modelmanager.coordinator.app import create_app

    auth.write_token("old-token")

    with TestClient(create_app(), headers={"Authorization": "Bearer old-token"}) as client:
        assert client.get("/v1/help").status_code == 200

        auth.write_token("new-token")

        assert client.get("/v1/help").status_code == 401  # old header, rotated token
        client.headers["Authorization"] = "Bearer new-token"
        assert client.get("/v1/help").status_code == 200
