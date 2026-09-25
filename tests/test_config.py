import json
from pathlib import Path

import pytest

from modelmanager.config import load_registry

EXAMPLE = Path(__file__).resolve().parent.parent / "registry.example.json"


def test_loads_example_registry():
    registry = load_registry(EXAMPLE)
    assert "nomic-embed" in registry.models
    assert registry.models["nomic-embed"].pinned is True
    assert registry.models["qwen-72b"].idle_timeout_seconds == 900
    assert len(registry.models["qwen-72b"].candidates) == 2


def test_rejects_unknown_engine_reference(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["models"]["qwen-72b"]["candidates"][0]["engine"] = "does-not-exist"
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
