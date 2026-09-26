from __future__ import annotations

import base64
import json

from .. import remote_paths
from ..runners.base import CommandResult
from ..runners.ssh import SSHRunner


def invoke(runner: SSHRunner, action: str, payload: dict) -> CommandResult:
    """Runs one action against scripts/windows/agent.ps1 over SSH, shared by
    any adapter that needs to manage a native Windows process (llama.cpp,
    or a self-managed 'ollama serve' for a custom build)."""
    script_path = remote_paths.windows_agent_script_path(runner)
    payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    cmd = (
        f'powershell -NoProfile -ExecutionPolicy Bypass -File {script_path} '
        f'-Action {action} -PayloadB64 {payload_b64}'
    )
    return runner.run_shell(cmd, timeout=60)
