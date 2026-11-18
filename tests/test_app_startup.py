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


def test_exact_out_of_scope_name_is_indistinguishable_from_truly_unknown(tmp_path, monkeypatch):
    # The exact bug the tier-5 adversarial smoke test found: an exact name
    # for a resource that exists but is out of scope used to 403, while a
    # substring guess for the same resource (or a name that truly doesn't
    # exist) 404'd - letting a grant confirm "this exact name exists" by
    # the status code alone. Every resource kind that has this exact-name
    # lookup shape (models, systems, engines, gpu_groups) must now return
    # the identical body for both cases.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {
            "sys-a": {"os": "linux", "control": "local"},
            "sys-b": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {"gpu-a": {"system": "sys-a"}, "gpu-b": {"system": "sys-b"}},
        "engines": {
            "eng-a": {"system": "sys-a", "type": "llama.cpp", "binary": "/bin/true"},
            "eng-b": {"system": "sys-b", "type": "llama.cpp", "binary": "/bin/true"},
        },
        "models": {
            "model-a": {"candidates": [{"engine": "eng-a", "gpu_group": "gpu-a", "port": 1, "model_path": "/m/a.gguf"}]},
            "model-b": {"candidates": [{"engine": "eng-b", "gpu_group": "gpu-b", "port": 2, "model_path": "/m/b.gguf"}]},
        },
    }))

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sys-a")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        for exists_path, bogus_path in (
            ("/v1/models/model-b", "/v1/models/totally-bogus-model"),
            ("/v1/systems/sys-b", "/v1/systems/totally-bogus-system"),
            ("/v1/engines/eng-b", "/v1/engines/totally-bogus-engine"),
            ("/v1/gpu_groups/gpu-b", "/v1/gpu_groups/totally-bogus-gpu-group"),
        ):
            exists_resp = client.get(exists_path)
            bogus_resp = client.get(bogus_path)
            assert exists_resp.status_code == 404, exists_path
            assert bogus_resp.status_code == 404, bogus_path
            # Same shape of body too (not just the same status code) -
            # only the quoted name differs, which the caller already typed
            # themselves either way.
            assert exists_resp.json()["code"] == bogus_resp.json()["code"] == "NOT_FOUND"


def test_grant_token_is_accepted_and_scoped_to_its_systems(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {
            "sys-a": {"os": "linux", "control": "local"},
            "sys-b": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {}, "engines": {}, "models": {},
    }))

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sys-a")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        allowed = client.get("/v1/systems/sys-a")
        forbidden = client.get("/v1/systems/sys-b")
        listed = client.get("/v1/systems")
        write_attempt = client.post("/v1/systems/sys-a", json={"os": "linux", "control": "local"})

    assert allowed.status_code == 200
    # 404, not 403 - see test_grant_filters_dump_all_endpoints_...'s own
    # comment on why an exact out-of-scope name must look identical to one
    # that doesn't exist.
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "NOT_FOUND"
    assert set(listed.json()) == {"sys-a"}
    assert write_attempt.status_code == 403
    assert write_attempt.json()["code"] == "FORBIDDEN"


def test_expired_system_grant_is_rejected(tmp_path, monkeypatch):
    # The actual enforcement point for grant allow --expires-in - everything
    # else (parsing the duration, storing the timestamp) is only correct if
    # a request against an already-past expiry really does get rejected.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {
            "sys-a": {"os": "linux", "control": "local"},
            "sys-b": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {}, "engines": {}, "models": {},
    }))

    from datetime import datetime, timedelta, timezone

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sys-a")  # standing access, never expires
    grants.allow_system(
        "bob", "sys-b",
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        still_allowed = client.get("/v1/systems/sys-a")
        expired = client.get("/v1/systems/sys-b")
        listed = client.get("/v1/systems")

    assert still_allowed.status_code == 200
    assert expired.status_code == 404  # same no-echo reasoning as the exact-name tests above
    assert set(listed.json()) == {"sys-a"}


def test_not_yet_expired_system_grant_is_still_accepted(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {"sys-a": {"os": "linux", "control": "local"}},
        "gpu_groups": {}, "engines": {}, "models": {},
    }))

    from datetime import datetime, timedelta, timezone

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system(
        "bob", "sys-a",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
    )

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        resp = client.get("/v1/systems/sys-a")

    assert resp.status_code == 200


def test_grant_filters_dump_all_endpoints_and_blocks_model_actions_outside_scope(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {
            "sys-a": {"os": "linux", "control": "local"},
            "sys-b": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {"gpu-a": {"system": "sys-a"}, "gpu-b": {"system": "sys-b"}},
        "engines": {
            "eng-a": {"system": "sys-a", "type": "llama.cpp", "binary": "/bin/true"},
            "eng-b": {"system": "sys-b", "type": "llama.cpp", "binary": "/bin/true"},
        },
        "models": {
            "model-a": {"candidates": [{"engine": "eng-a", "gpu_group": "gpu-a", "port": 1, "model_path": "/m/a.gguf"}]},
            "model-b": {"candidates": [{"engine": "eng-b", "gpu_group": "gpu-b", "port": 2, "model_path": "/m/b.gguf"}]},
        },
        # g1 mixes an allowed and a forbidden model - should be hidden from
        # bob entirely (see _group_visible); g2 is fully within bob's scope
        # and should still show up normally.
        "groups": {"g1": ["model-a", "model-b"], "g2": ["model-a"]},
    }))

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sys-a")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        config = client.get("/v1/config").json()
        models = client.get("/v1/models").json()
        groups = client.get("/v1/groups").json()
        mixed_group_get = client.get("/v1/groups/g1")
        allowed_group_get = client.get("/v1/groups/g2")
        allowed_get = client.get("/v1/models/model-a")
        forbidden_get = client.get("/v1/models/model-b")
        forbidden_ensure = client.post("/v1/models/model-b/ensure")
        forbidden_logs = client.get("/v1/models/model-b/logs")
        doctor_attempt = client.get("/v1/doctor")
        all_stats = client.get("/v1/systems/stats").json()

    assert set(config["systems"]) == {"sys-a"}
    assert set(config["models"]) == {"model-a"}
    assert [m["name"] for m in models] == ["model-a"]
    assert "g1" not in groups  # mixed membership - hidden entirely, not partially filtered
    assert groups["g2"] == ["model-a"]
    assert mixed_group_get.status_code == 404
    assert allowed_group_get.status_code == 200
    assert {r["system"] for r in all_stats["results"]} == {"sys-a"}
    assert allowed_get.status_code == 200
    # 404, not 403: an exact out-of-scope name must look identical to one
    # that doesn't exist at all - bob can't learn "model-b exists but I
    # can't see it" (a real existence oracle, found by the tier-5
    # adversarial smoke test) vs "there's no such model".
    assert forbidden_get.status_code == 404
    assert forbidden_ensure.status_code == 404
    assert forbidden_logs.status_code == 404
    assert doctor_attempt.status_code == 403  # owner-only, regardless of allowed_systems
    # Same body as a genuine miss, and must not reveal which system
    # actually backs it (sys-b) either.
    for resp in (forbidden_get, forbidden_ensure, forbidden_logs):
        assert resp.json() == {"error": "unknown model 'model-b'", "code": "NOT_FOUND"}


def test_ambiguous_model_name_never_leaks_out_of_scope_models_to_a_grant(tmp_path, monkeypatch):
    # The actual HTTP-level regression test for the cross-scope disclosure:
    # Manager.resolve_name() used to substring-match against the *whole*
    # registry before any grant-scope check ran, so a grant could send a
    # short substring and read other models' real names straight out of a
    # 409 AmbiguousNameError - even models on systems it has zero access to,
    # exactly what _require_reachable's own no-echo rule says is withheld
    # elsewhere. "shared-a" and "shared-b" both contain "shared".
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {
            "sys-a": {"os": "linux", "control": "local"},
            "sys-b": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {"gpu-a": {"system": "sys-a"}, "gpu-b": {"system": "sys-b"}},
        "engines": {
            "eng-a": {"system": "sys-a", "type": "llama.cpp", "binary": "/bin/true"},
            "eng-b": {"system": "sys-b", "type": "llama.cpp", "binary": "/bin/true"},
        },
        "models": {
            "shared-a": {"candidates": [{"engine": "eng-a", "gpu_group": "gpu-a", "port": 1, "model_path": "/m/a.gguf"}]},
            "shared-b": {"candidates": [{"engine": "eng-b", "gpu_group": "gpu-b", "port": 2, "model_path": "/m/b.gguf"}]},
        },
    }))

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sys-a")  # shared-a only - never shared-b

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        ambiguous_for_owner = client.post(
            "/v1/models/shared/ensure", headers={"Authorization": "Bearer owner-token"}
        )
        scoped_probe = client.post("/v1/models/shared/ensure")
        unique_in_scope = client.post("/v1/models/shared-a/ensure")

    # The owner (unrestricted) genuinely sees both names - the ambiguity
    # itself isn't the bug, only leaking it across a grant's scope boundary.
    assert ambiguous_for_owner.status_code == 409
    assert "shared-a" in ambiguous_for_owner.json()["error"]
    assert "shared-b" in ambiguous_for_owner.json()["error"]

    # bob's own substring resolves to exactly one in-scope candidate, so it
    # must succeed outright instead of raising an ambiguity error at all -
    # and shared-b must never appear anywhere in the response.
    assert scoped_probe.status_code == 200
    assert "shared-b" not in scoped_probe.text

    assert unique_in_scope.status_code == 200


def test_ambiguous_model_name_with_no_in_scope_match_looks_like_not_found(tmp_path, monkeypatch):
    # If the *only* models an ambiguous substring matches are all out of a
    # grant's scope, that must look identical to "nothing matched" (404) -
    # never a 409 naming models the grant can't see.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {
            "sys-a": {"os": "linux", "control": "local"},
            "sys-b": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {"gpu-a": {"system": "sys-a"}, "gpu-b": {"system": "sys-b"}},
        "engines": {
            "eng-a": {"system": "sys-a", "type": "llama.cpp", "binary": "/bin/true"},
            "eng-b": {"system": "sys-b", "type": "llama.cpp", "binary": "/bin/true"},
        },
        "models": {
            "visible": {"candidates": [{"engine": "eng-a", "gpu_group": "gpu-a", "port": 1, "model_path": "/m/a.gguf"}]},
            "hidden-x": {"candidates": [{"engine": "eng-b", "gpu_group": "gpu-b", "port": 2, "model_path": "/m/x.gguf"}]},
            "hidden-y": {"candidates": [{"engine": "eng-b", "gpu_group": "gpu-b", "port": 3, "model_path": "/m/y.gguf"}]},
        },
    }))

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sys-a")  # visible only, never hidden-x/hidden-y

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        resp = client.get("/v1/models/hidden/dry-run")

    assert resp.status_code == 404
    assert "hidden-x" not in resp.text
    assert "hidden-y" not in resp.text


def test_revoked_grant_cannot_use_ambiguous_names_to_enumerate_models(tmp_path, monkeypatch):
    # grant revoke-all keeps the identity/token valid (by design - see
    # grants.revoke_all) while zeroing allowed_systems. That must not leave
    # an enumeration side-channel: a fully revoked token sending a short
    # substring must see "not found", never a list of real model names.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {"sys-a": {"os": "linux", "control": "local"}},
        "gpu_groups": {"gpu-a": {"system": "sys-a"}},
        "engines": {"eng-a": {"system": "sys-a", "type": "llama.cpp", "binary": "/bin/true"}},
        "models": {
            "model-b": {"candidates": [{"engine": "eng-a", "gpu_group": "gpu-a", "port": 1, "model_path": "/m/b.gguf"}]},
            "model-z": {"candidates": [{"engine": "eng-a", "gpu_group": "gpu-a", "port": 2, "model_path": "/m/z.gguf"}]},
        },
    }))

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sys-a")
    grants.revoke_all()

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        resp = client.post("/v1/models/model-/ensure")

    assert resp.status_code == 404
    assert "model-b" not in resp.text
    assert "model-z" not in resp.text


def _write_scoped_registry(registry_path):
    # "shared" has candidate 0 on vault (never granted) and candidate 1 on
    # sleek (granted) - the same shape the any-candidate union test used to
    # let slip through to whichever candidate actually happened to be
    # active/selected. "needs-vault-dep" depends on a vault-only model.
    # "mixed" groups an allowed and a disallowed-system model together.
    registry_path.write_text(json.dumps({
        "systems": {
            "sleek": {"os": "linux", "control": "local"},
            "vault": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {
            "gpu-sleek": {"system": "sleek"},
            "gpu-vault": {"system": "vault"},
        },
        "engines": {
            "eng-sleek": {"system": "sleek", "type": "llama.cpp", "binary": "/bin/true"},
            "eng-vault": {"system": "vault", "type": "llama.cpp", "binary": "/bin/true"},
        },
        "models": {
            "shared": {"candidates": [
                {"engine": "eng-vault", "gpu_group": "gpu-vault", "port": 1, "model_path": "/m/shared-vault.gguf"},
                {"engine": "eng-sleek", "gpu_group": "gpu-sleek", "port": 2, "model_path": "/m/shared-sleek.gguf"},
            ]},
            "dep-on-vault": {"candidates": [
                {"engine": "eng-vault", "gpu_group": "gpu-vault", "port": 3, "model_path": "/m/dep.gguf"},
            ]},
            "needs-vault-dep": {
                "depends_on": ["dep-on-vault"],
                "candidates": [{"engine": "eng-sleek", "gpu_group": "gpu-sleek", "port": 4, "model_path": "/m/needs.gguf"}],
            },
            # Engine on sleek (granted) but gpu_group on vault (not granted) -
            # legal per the schema, but its one candidate fails the AND-based
            # _candidate_allowed check, so is_reachable is false and the whole
            # model must be hidden, not just shown with an empty candidates list.
            "mismatched": {
                "candidates": [{"engine": "eng-sleek", "gpu_group": "gpu-vault", "port": 5, "model_path": "/m/mismatched.gguf"}],
            },
        },
        "groups": {"mixed": ["shared", "dep-on-vault"]},
    }))


def test_config_strips_out_of_scope_candidates_instead_of_the_whole_model(tmp_path, monkeypatch):
    # "shared" passes the any-candidate union test (its sleek candidate is
    # allowed), but its vault candidate's engine/model_path/port must never
    # show up in a response to a grant with no access to vault.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        config = client.get("/v1/config").json()

    shared = config["models"]["shared"]
    assert [c["engine"] for c in shared["candidates"]] == ["eng-sleek"]
    assert "shared-vault.gguf" not in json.dumps(config)
    assert "eng-vault" not in config["models"]


def test_config_hides_a_model_whose_dependency_is_entirely_out_of_scope(tmp_path, monkeypatch):
    # "needs-vault-dep" has its own candidate on sleek, but depends_on names
    # "dep-on-vault", which is entirely out of scope - a model that can
    # never actually be ensured for this grant (its dependency can't be
    # satisfied) is hidden entirely (Manager.is_reachable), same as a group
    # with any out-of-scope member, rather than shown as a non-functional
    # entry with its depends_on silently emptied.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        config = client.get("/v1/config").json()

    assert "dep-on-vault" not in config["models"]
    assert "needs-vault-dep" not in config["models"]


def test_config_hides_a_model_whose_only_candidate_has_mismatched_systems(tmp_path, monkeypatch):
    # "mismatched" has its engine on sleek (granted) but its gpu_group on
    # vault (not granted) - that one candidate fails the AND-based
    # _candidate_allowed check entirely, so the model has zero candidates
    # actually usable by this grant. Hidden outright (is_reachable), rather
    # than shown with an empty candidates list.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        config = client.get("/v1/config").json()

    assert "mismatched" not in config["models"]
    assert "mismatched.gguf" not in json.dumps(config)


def test_list_models_hides_unreachable_models_not_just_out_of_scope_ones(tmp_path, monkeypatch):
    # GET /v1/models must apply the same is_reachable test GET /v1/config
    # does - a model isn't listed just because it has *a* candidate in
    # scope, if its dependency chain or its own candidate's system mismatch
    # means it could never actually be ensured.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        names = {m["name"] for m in client.get("/v1/models").json()}

    assert names == {"shared"}
    assert "needs-vault-dep" not in names
    assert "dep-on-vault" not in names
    assert "mismatched" not in names


def test_ensure_on_a_candidate_with_mismatched_engine_and_gpu_group_systems_is_forbidden(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        resp = client.post("/v1/models/mismatched/ensure")

    # 404, not 403 - "mismatched" is unreachable for bob (its one candidate
    # fails the engine+gpu_group AND check), so _require_reachable's gate
    # fires before the route ever calls ensure() - same no-echo reasoning
    # as every other exact-name case above.
    assert resp.status_code == 404
    assert "vault" not in resp.text


def test_release_on_out_of_scope_active_candidate_is_forbidden(tmp_path, monkeypatch):
    # stop() already 403s against shared's vault candidate for bob - release()
    # must too, or bob can clear the owner's lease on a model actively
    # running on a system bob has no access to.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")  # not vault

    with TestClient(create_app()) as client:
        client.headers = {"Authorization": "Bearer owner-token"}
        acquired = client.post("/v1/models/shared/acquire")  # picks vault, candidate 0
        assert acquired.status_code == 200
        lease_id = acquired.json()["lease_id"]

        client.headers = {"Authorization": "Bearer bobs-token"}
        release_resp = client.post("/v1/models/shared/release", params={"lease_id": lease_id})

    assert release_resp.status_code == 403
    assert "vault" not in release_resp.text


def test_ensure_on_a_dependency_outside_scope_is_forbidden_and_starts_nothing(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")  # not vault - needs-vault-dep's dependency lives only there

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        resp = client.post("/v1/models/needs-vault-dep/ensure")

    # 404, not 403 - needs-vault-dep is unreachable for bob (its dependency
    # isn't), so _require_reachable's gate fires before the route ever
    # calls ensure() - same no-echo reasoning as the mismatched-candidate
    # test above. Checking for the *dependency's* name, not "vault" as a
    # raw substring - "needs-vault-dep" is the name bob himself asked for,
    # so it containing "vault" isn't a leak of anything new.
    assert resp.status_code == 404
    assert resp.json() == {"error": "unknown model 'needs-vault-dep'", "code": "NOT_FOUND"}
    assert "dep-on-vault" not in resp.text


def test_group_action_and_stats_routes_404_a_mixed_group_like_get_does(tmp_path, monkeypatch):
    # GET /v1/groups/{name} already 404s "mixed" for bob (it has a
    # vault-only member). Every action/stats route on the same group name
    # must match that, rather than recovering the hidden membership through
    # a per-member "not permitted" breakdown.
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    _write_scoped_registry(registry_path)

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        get_resp = client.get("/v1/groups/mixed")
        ensure_resp = client.post("/v1/groups/mixed/ensure")
        dry_run_resp = client.get("/v1/groups/mixed/dry-run")
        verify_resp = client.post("/v1/groups/mixed/verify")
        stop_resp = client.post("/v1/groups/mixed/stop")
        stats_resp = client.get("/v1/groups/mixed/stats")

    assert get_resp.status_code == 404
    for resp in (ensure_resp, dry_run_resp, verify_resp, stop_resp, stats_resp):
        assert resp.status_code == 404
        assert "dep-on-vault" not in resp.text


def test_group_stats_never_reports_a_system_outside_the_grants_scope(tmp_path, monkeypatch):
    # A group that's fully visible (every member in scope via the broad
    # any-candidate test) can still have a member whose *active* candidate
    # sits on an out-of-scope system - group_stats must never report that
    # system's figures just because the model itself was "in scope".
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "systems": {
            "sleek": {"os": "linux", "control": "local"},
            "vault": {"os": "linux", "control": "local"},
        },
        "gpu_groups": {
            "gpu-sleek": {"system": "sleek"},
            "gpu-vault": {"system": "vault"},
        },
        "engines": {
            "eng-sleek": {"system": "sleek", "type": "llama.cpp", "binary": "/bin/true"},
            "eng-vault": {"system": "vault", "type": "llama.cpp", "binary": "/bin/true"},
        },
        "models": {
            "shared": {"candidates": [
                {"engine": "eng-vault", "gpu_group": "gpu-vault", "port": 1, "model_path": "/m/shared-vault.gguf"},
                {"engine": "eng-sleek", "gpu_group": "gpu-sleek", "port": 2, "model_path": "/m/shared-sleek.gguf"},
            ]},
        },
        "groups": {"solo": ["shared"]},
    }))

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")
    grants.allow_system("bob", "sleek")

    with TestClient(create_app(), headers={"Authorization": "Bearer owner-token"}) as owner_client:
        # Owner activates "shared" on its first (vault) candidate.
        owner_client.post("/v1/models/shared/ensure")

    with TestClient(create_app(), headers={"Authorization": "Bearer bobs-token"}) as client:
        # The group itself is still visible (shared has a sleek candidate).
        get_resp = client.get("/v1/groups/solo")
        stats_resp = client.get("/v1/groups/solo/stats")

    assert get_resp.status_code == 200
    systems_reported = {r["system"] for r in stats_resp.json()["results"]}
    assert systems_reported == set()  # shared's active candidate (vault) is out of scope


def test_unmatched_token_is_still_rejected_even_with_grants_configured(tmp_path, monkeypatch):
    _minimal_app_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELMANAGER_TOKEN", "owner-token")

    from modelmanager import grants
    from modelmanager.coordinator.app import create_app

    grants.add_grant("bob", "bobs-token")

    with TestClient(create_app(), headers={"Authorization": "Bearer someone-elses-token"}) as client:
        resp = client.get("/v1/help")

    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


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
