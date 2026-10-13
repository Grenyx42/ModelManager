---
name: modelmanager
description: Control ModelManager - start/stop/verify LLM models and edit its systems/engines/gpu_groups/models registry via REST/CLI.
---

# ModelManager

Self-describing: call `modelmanager help` (or `GET /v1/help`) first for the
live schema, endpoints, and create order (systems -> engines/gpu_groups ->
models). Trust that over this file.

**Connect:** CLI defaults to `https://127.0.0.1:8778` (override via
`MODELMANAGER_URL`) and reads its token/TLS cert from
`~/.config/modelmanager/` automatically - just run `modelmanager ...`. A
remote REST caller needs that same base URL + `Authorization: Bearer
<token>` + the cert; ask the user if you don't have them.

**Workflow:** `doctor --pretty` (no-op health check) -> `list` -> `ensure
<name>` / `acquire <name> --ttl N` to use a model -> `put <kind> <name>
'<json>'` or hand-edit registry.json + `reload` to change config.
`validate @file` checks a registry before applying it.

**Never invent** a system/engine name, path, or port - read `config` (live)
or `registry.example.json` (shape), or ask.
