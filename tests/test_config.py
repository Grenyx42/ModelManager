import json
from pathlib import Path

import pytest

from modelmanager.config import EngineConfig, Registry, load_registry

EXAMPLE = Path(__file__).resolve().parent.parent / "registry.example.json"
CLEAN = Path(__file__).resolve().parent.parent / "registry.clean.json"


def _minimal_registry_dict(engines: dict, models: dict) -> dict:
    return {
        "systems": {"sleek": {"os": "linux", "control": "local"}},
        "gpu_groups": {"gpu0": {"system": "sleek"}, "gpu1": {"system": "sleek"}},
        "engines": engines,
        "models": models,
    }


def test_loads_example_registry():
    registry = load_registry(EXAMPLE)
    assert "nomic-embed" in registry.models
    # Deliberately not pinned - see registry.example.json's comment: a
    # pinned model auto-starts at coordinator boot, which would fail loudly
    # on a brand-new install before anything's been edited to match real
    # machines.
    assert registry.models["nomic-embed"].pinned is False
    assert registry.models["bonsai-27b"].idle_timeout_seconds == 21600
    assert len(registry.models["bonsai-27b"].candidates) == 1


def test_loads_clean_registry():
    # What scripts/setup.sh actually installs as a fresh registry.json -
    # must stay empty-but-valid, since a non-empty default would mean
    # guessing at machines/models the new user doesn't actually have.
    registry = load_registry(CLEAN)
    assert registry.systems == {}
    assert registry.gpu_groups == {}
    assert registry.engines == {}
    assert registry.models == {}


def test_engine_env_defaults_empty_and_can_be_set():
    default_engine = EngineConfig(system="sleek", type="llama.cpp", binary="/opt/llama-server")
    assert default_engine.env == {}

    engine_with_env = EngineConfig(
        system="sleek", type="llama.cpp", binary="/opt/llama-server", env={"LD_LIBRARY_PATH": "/opt/cuda/lib"}
    )
    assert engine_with_env.env == {"LD_LIBRARY_PATH": "/opt/cuda/lib"}


def test_rejects_unknown_engine_reference(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["models"]["bonsai-27b"]["candidates"][0]["engine"] = "does-not-exist"
    bad_path = tmp_path / "registry.json"
    bad_path.write_text(json.dumps(data))
    with pytest.raises(Exception):
        load_registry(bad_path)


def test_rejects_ssh_system_without_host(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["systems"]["monolith"] = {"os": "windows", "control": "ssh"}
    bad_path = tmp_path / "registry.json"
    bad_path.write_text(json.dumps(data))
    with pytest.raises(Exception):
        load_registry(bad_path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_registry(tmp_path / "nope.json")


def test_allows_two_llamacpp_candidates_in_different_gpu_groups_sharing_a_port():
    # Different engines/builds, different gpu_groups (so both COULD run at
    # once) declaring the same port - no longer a registry-time concern,
    # since llama.cpp/process ports are resolved live at start() time (see
    # port_check.resolve_port) if they actually collide.
    data = _minimal_registry_dict(
        engines={
            "eng-a": {"system": "sleek", "type": "llama.cpp", "binary": "/bin/true"},
            "eng-b": {"system": "sleek", "type": "llama.cpp", "binary": "/bin/false"},
        },
        models={
            "model-a": {"candidates": [{"engine": "eng-a", "gpu_group": "gpu0", "port": 8080, "model_path": "/m/a.gguf"}]},
            "model-b": {"candidates": [{"engine": "eng-b", "gpu_group": "gpu1", "port": 8080, "model_path": "/m/b.gguf"}]},
        },
    )
    registry = Registry.model_validate(data)
    assert set(registry.models) == {"model-a", "model-b"}


def test_rejects_two_different_ollama_engines_sharing_a_port():
    data = _minimal_registry_dict(
        engines={
            "ollama-a": {"system": "sleek", "type": "ollama", "base_url": "http://localhost:11434"},
            "ollama-b": {"system": "sleek", "type": "ollama", "base_url": "http://localhost:11434"},
        },
        models={
            "model-a": {"candidates": [{"engine": "ollama-a", "gpu_group": "gpu0", "port": 11434, "model_ref": "a"}]},
            "model-b": {"candidates": [{"engine": "ollama-b", "gpu_group": "gpu1", "port": 11434, "model_ref": "b"}]},
        },
    )
    with pytest.raises(Exception, match="claimed by more than one ollama engine"):
        Registry.model_validate(data)


def test_allows_two_models_sharing_the_same_ollama_engine_and_port():
    data = _minimal_registry_dict(
        engines={"ollama-a": {"system": "sleek", "type": "ollama", "base_url": "http://localhost:11434"}},
        models={
            "model-a": {"candidates": [{"engine": "ollama-a", "gpu_group": "gpu0", "port": 11434, "model_ref": "a"}]},
            "model-b": {"candidates": [{"engine": "ollama-a", "gpu_group": "gpu0", "port": 11434, "model_ref": "b"}]},
        },
    )
    registry = Registry.model_validate(data)  # must not raise - one ollama server multiplexing two models
    assert set(registry.models) == {"model-a", "model-b"}


def test_rejects_ollama_candidate_port_mismatched_from_base_url():
    data = _minimal_registry_dict(
        engines={"ollama-a": {"system": "sleek", "type": "ollama", "base_url": "http://localhost:11434"}},
        models={"model-a": {"candidates": [{"engine": "ollama-a", "gpu_group": "gpu0", "port": 9999, "model_ref": "a"}]}},
    )
    with pytest.raises(Exception, match="doesn't match engine"):
        Registry.model_validate(data)


def _depends_on_registry(**depends_on_by_model) -> dict:
    engines = {"eng": {"system": "sleek", "type": "process", "binary": "/bin/true"}}
    models = {
        name: {
            "candidates": [{"engine": "eng", "gpu_group": "gpu0", "port": 8080 + i, "args": []}],
            "depends_on": depends_on_by_model.get(name, []),
        }
        for i, name in enumerate(depends_on_by_model)
    }
    return _minimal_registry_dict(engines=engines, models=models)


def test_depends_on_unknown_model_is_rejected():
    data = _depends_on_registry(a=["ghost"])
    with pytest.raises(Exception, match="depends_on unknown model"):
        Registry.model_validate(data)


def test_depends_on_self_reference_is_rejected():
    data = _depends_on_registry(a=["a"])
    with pytest.raises(Exception, match="cannot depend on itself"):
        Registry.model_validate(data)


def test_depends_on_cycle_is_rejected():
    data = _depends_on_registry(a=["b"], b=["a"])
    with pytest.raises(Exception, match="cycle"):
        Registry.model_validate(data)


def test_depends_on_valid_chain_is_accepted():
    data = _depends_on_registry(a=["b"], b=["c"], c=[])
    registry = Registry.model_validate(data)
    assert registry.models["a"].depends_on == ["b"]
    assert registry.models["c"].depends_on == []
