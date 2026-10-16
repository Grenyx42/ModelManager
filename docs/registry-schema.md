# Registry schema

Field-level details beyond what's already in `registry.example.json` and
`modelmanager help`. See the main [README](../README.md#registry) for
where `registry.json` lives and how it's created.

None of `registry.example.json`'s models are `pinned`, deliberately - a
pinned model auto-starts the moment the coordinator boots (see the
acquire/release/pinned notes in [Groups](cli-reference.md#groups)), which
would fail loudly on a brand-new install before you've edited anything to
match your own machines. Once you've pointed a model at something real, a
standing dependency like an always-on embedding model looks like this:

```json
"nomic-embed": {
  "pinned": true,
  "candidates": [
    { "engine": "ollama-systemA", "model_ref": "nomic-embed-text", "port": 11434, "gpu_group": "systemA-gpu0-alwaysOn", "ollama_task": "embed" }
  ]
}
```

An engine can set `"env": {...}` - extra environment variables merged in
when the coordinator launches its process - and `"cwd": "..."` - the
working directory to launch it from, if that matters for the build (e.g. it
resolves a relative library path from there). For llama.cpp this always
applies. Common `env` use: `LD_LIBRARY_PATH`, when a build's CUDA/cuBLAS
libraries live somewhere non-standard (e.g. bundled inside a Python venv via
a pip `nvidia-*` package rather than installed system-wide) - the
coordinator launches processes with its own environment, not whatever a dev
shell happened to have set.

An `ollama` engine normally just talks to `base_url`, assuming a server is
already running there (a system/Windows service, outside our control). Set
`"binary"` (and usually `"cwd"`/`"env"`) on it too if that's not true - e.g.
a custom Ollama build that isn't registered as a service anywhere - and the
coordinator launches and manages `ollama serve` itself the first time a
model on that engine is started, the same way it manages a raw llama.cpp
process (PID/log file locally, or the Windows WMI-launcher agent over SSH).
`base_url` still needs to be a host the coordinator can actually reach
(e.g. `http://systemB:11434`, not `localhost`, when the server lives on a
different box), and `OLLAMA_HOST` in `env` needs to match the port and bind
address you want it listening on.
