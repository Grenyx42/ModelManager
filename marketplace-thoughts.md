# Model Manager --- V1 Marketplace Readiness & Implementation Plan

## 1. Purpose

Model Manager is a lightweight distributed orchestration layer for local
AI models and inference services.

The core problem it solves is not model downloading or providing another
inference runtime. It manages heterogeneous model runtimes across
multiple machines and gives humans, applications, and coding agents a
consistent way to bring required AI services online, verify that they
are actually usable, and discover their endpoints.

### Core V1 promise

> Declare the local AI services you need. Model Manager starts them
> wherever they live, verifies that they are actually answering
> inference requests, and returns machine-readable endpoints to the
> caller.

This is especially useful when: - Models are distributed across multiple
machines because no single machine has enough VRAM. - Different models
require different runtimes or startup commands. - A workflow needs
several models simultaneously. - Applications or coding agents need to
programmatically ensure dependencies are available before continuing. -
Linux and Windows inference hosts need to coexist. - The operator does
not want to manually SSH into machines, start processes, inspect logs,
and track ports.

------------------------------------------------------------------------

## 2. Existing Architecture

### Central manager

One primary machine owns the Model Manager definitions and runs a small
REST service. The CLI communicates with this REST service rather than
independently orchestrating hosts.

### Remote execution

The manager reaches inference machines over SSH.

Linux hosts can generally execute model/runtime commands directly.
Windows hosts can receive helper scripts that Model Manager deploys and
invokes remotely, avoiding long chains of individual remote commands.

### Declarative definitions

JSON definitions describe: - Hosts/machines. - Models available on each
host. - Runtime/startup commands. - Model groups. - Host pinning and
placement. - Connection/end-point information.

### Groups

Multiple models can be grouped into a logical stack. A single request
can start the complete group across several machines.

Example use case:

A Pi coding experiment may require: 1. A reasoning/coding model on a
3090 machine. 2. An embedding model on another machine. 3. A third
specialist model on a dual-2080 Ti machine.

One group command starts all three and verifies the complete stack.

### `ensure`

`ensure` does more than check whether a process exists. It brings the
requested model/service online and waits until it is usable.

Readiness includes an actual inference request using a simple prompt
expected to return an `OK`-style response. This verifies the inference
service rather than merely checking that a TCP port is open.

### `verify`

Verification can operate on individual models or groups and confirm that
every required inference service is healthy.

### Machine-readable output

CLI output is JSON by default.

A `-p` / pretty option formats that JSON for convenient human viewing.

This is an important design decision: automation and agent use are
first-class rather than being added later.

------------------------------------------------------------------------

# 3. Phase-One Marketplace Requirements

The following items should be considered before the first public
Marketplace release.

## 3.1 Structured Error Handling and Diagnostics

### What it does

Return specific, machine-readable failure information instead of a
generic "model failed to start."

The system should distinguish failures such as: - Host unreachable. -
SSH authentication failure. - Required runtime missing. - Startup
command failure. - Port already occupied. - Process started but
exited. - Model load timeout. - Insufficient VRAM/RAM when detectable. -
HTTP endpoint unavailable. - Inference verification failed. - Dependency
failed. - Group partially started.

Example conceptual response:

``` json
{
  "ready": false,
  "model": "qwen-coder",
  "host": "gpu3090",
  "error": {
    "code": "INFERENCE_VERIFY_FAILED",
    "stage": "verify",
    "message": "Process is running but inference verification timed out"
  }
}
```

### What it solves

Without structured errors, both users and agents must inspect logs
manually to determine whether the problem is SSH, runtime startup,
networking, model loading, or inference.

### Why Phase One needs it

Public users will have environments unlike the developer's machines.
Failure quality will strongly determine whether the plugin feels
dependable.

This is also essential for coding agents because they need stable error
codes they can reason about programmatically.

### Suggested implementation phases

1.  Define a stable error envelope and error-code enumeration.
2.  Divide startup into explicit stages: resolve → connect → launch →
    endpoint → verify.
3.  Convert known failures into structured errors.
4.  Preserve raw command/runtime output as diagnostic detail without
    making callers parse it.
5.  Add group-level aggregation of child errors.

------------------------------------------------------------------------

## 3.2 Port and Runtime Instance Management

### What it does

Manage runtime instances and ports rather than assuming one runtime
equals one model.

Examples: - One Ollama instance may serve several compatible models. -
Two llama.cpp instances may need different ports. - Multiple independent
instances may be required on the same host. - A configured/default port
may already be occupied.

The returned endpoint becomes part of the service contract.

### What it solves

Static ports work on a small private setup but quickly collide once
multiple models, agents, or users operate concurrently.

It also separates the concepts of: - Host. - Runtime. - Runtime
instance. - Model/service. - Endpoint.

### Why Phase One needs it

Endpoint ambiguity would make the otherwise excellent machine-readable
interface unreliable.

At minimum, V1 needs deterministic conflict detection and correct
endpoint reporting.

### Suggested implementation phases

1.  Represent runtime instances explicitly.
2.  Detect configured port conflicts before launch.
3.  Return the actual host/port for every ready service.
4.  Support multiple instances on one host.
5.  Later: add safe automatic/dynamic port allocation.

Automatic randomized/dynamic port selection can be incremental, but
correct instance identity and endpoint reporting belong in V1.

------------------------------------------------------------------------

## 3.3 Stable Agent/Application Contract

### What it does

Make `ensure` a reliable API primitive for other software.

A caller should be able to request a model or group and receive
everything needed to use it without understanding the infrastructure.

Conceptual example:

``` json
{
  "ready": true,
  "group": "pi-coding-stack",
  "services": {
    "reasoning": {
      "host": "gpu3090",
      "port": 8081,
      "endpoint": "http://gpu3090:8081",
      "verified": true
    },
    "embedding": {
      "host": "dual2080",
      "port": 8082,
      "endpoint": "http://dual2080:8082",
      "verified": true
    }
  }
}
```

### What it solves

Pi, Codex, scripts, editors, and experiments should not need hard-coded
knowledge of where models run.

### Why Phase One needs it

This may be Model Manager's strongest differentiator. The application
asks for a capability; Model Manager handles infrastructure and returns
usable endpoints.

### Suggested implementation phases

1.  Define versioned JSON schemas for successful `ensure`, `verify`,
    `status`, and error responses.
2.  Guarantee endpoint fields and readiness semantics.
3.  Document exit codes/HTTP status behavior.
4.  Add an API/schema version field so the contract can evolve later
    without silently breaking clients.

------------------------------------------------------------------------

## 3.4 Dependency Resolution

### What it does

Allow a model, service, or group to declare other services it requires.

Example:

``` text
coding-stack
 ├── qwen-coder
 ├── nomic-embed
 └── reranker
```

Or a service itself may declare a dependency on another service.

### What it solves

Users should not have to remember startup ordering or manually construct
every combination as a separate group.

### Why Phase One needs it

Model Manager is already being used to ensure application dependencies
exist. Explicit dependencies make that behavior declarative and
reusable.

### Suggested implementation phases

1.  Allow definitions to contain `dependsOn`.
2.  Resolve dependencies recursively.
3.  Detect cycles before performing changes.
4.  Ensure dependencies before dependents.
5.  Aggregate dependency failures into the caller's JSON result.
6.  Later: add optional/alternative dependencies.

------------------------------------------------------------------------

## 3.5 Concurrency and Operation Locking

### What it does

Prevent multiple clients from racing to manipulate the same
model/runtime simultaneously.

Example:

Two coding agents call:

``` text
ensure qwen-coder
```

at nearly the same time.

Correct behavior is one startup operation. Both callers ultimately
receive the same healthy endpoint.

### What it solves

Without locking: - Duplicate processes may start. - Ports may collide. -
GPU memory can be exhausted. - One operation may stop something another
is starting. - State can disagree with reality.

### Why Phase One needs it

Once published, concurrent automation becomes likely. A REST service
also makes concurrency inherent rather than exceptional.

### Suggested implementation phases

1.  Add per-service/per-runtime operation locks.
2.  Make `ensure` idempotent.
3.  Let later callers join/wait on an in-progress startup.
4.  Add operation timeout handling.
5.  Return operation IDs/state where useful.
6.  Test simultaneous ensure/start/stop calls.

------------------------------------------------------------------------

## 3.6 Group Failure Semantics

### What it does

Define exactly what happens when only part of a group becomes ready.

Example:

``` text
reasoning    READY
embedding    READY
reranker     FAILED — host unreachable
```

The response should clearly report `2/3` ready and why the third failed.

Optionally support policies such as: - Keep successful services
running. - Roll back services started specifically for this request.

### What it solves

Distributed systems fail partially. Treating a group as a single opaque
success/failure hides the information needed to recover.

### Why Phase One needs it

Groups are already a core feature, so their failure behavior must be
predictable before external users depend on them.

### Suggested implementation phases

1.  Return per-member state in every group operation.
2.  Define overall group states such as READY, PARTIAL, FAILED.
3.  Add timeout behavior.
4.  Add an optional rollback-on-failure policy.
5.  Preserve pre-existing services during rollback; only undo changes
    made by the current operation.

------------------------------------------------------------------------

## 3.7 `status` / `doctor`

### What it does

Provide one command/API call that explains the complete health chain.

Useful checks include: - Manager REST service healthy. - Host
reachable. - SSH usable. - Runtime installed. - Process
expected/running. - Endpoint reachable. - Inference verification
succeeds. - Current host/port. - GPU/VRAM/RAM information when
available.

### What it solves

A distributed model setup otherwise requires SSH sessions, process
inspection, GPU tools, curl requests, and log reading.

### Why Phase One needs it

This will likely be the first command a new user runs when something
goes wrong.

### Suggested implementation phases

1.  Implement model-level status.
2.  Implement host-level status.
3.  Implement group-level status.
4.  Add `doctor` for deeper diagnostics.
5.  Ensure all output remains structured JSON with `-p` human
    formatting.

------------------------------------------------------------------------

## 3.8 Logs as a First-Class Operation

### What it does

Retrieve runtime logs without manually connecting to remote hosts.

Desired operations include conceptually:

``` text
model-manager logs qwen-coder
model-manager logs qwen-coder --follow
model-manager logs pi-coding-stack
```

### What it solves

When three models live on three machines, manually locating the right
process and log becomes significant friction.

### Why Phase One needs it

Logs are the fallback diagnostic path when structured errors do not
contain enough information.

### Suggested implementation phases

1.  Standardize where launched-process logs are captured.
2.  Retrieve recent logs remotely.
3.  Associate logs with model/runtime instance IDs.
4.  Add bounded/tail output.
5.  Later: add streaming/follow and aggregated group streaming.

Basic log retrieval belongs in V1; sophisticated multi-host streaming
can follow.

------------------------------------------------------------------------

## 3.9 Configuration Validation

### What it does

Validate definitions without launching anything.

Detect: - Unknown hosts. - Unknown group members. - Missing
dependencies. - Dependency cycles. - Duplicate/conflicting ports. -
Invalid commands. - Unsupported runtime/host combinations where known. -
Duplicate IDs. - Missing required fields.

### What it solves

Configuration errors should be found before Model Manager starts
changing remote machines.

### Why Phase One needs it

Declarative JSON is one of the product's strengths; a validator makes
that configuration safe and approachable.

### Suggested implementation phases

1.  Define a formal schema.
2.  Add schema validation.
3.  Add cross-reference validation.
4.  Add semantic checks such as port conflicts and cycles.
5.  Return all discovered validation errors in one pass where possible.

------------------------------------------------------------------------

## 3.10 Dry Run / Execution Plan

### What it does

Show exactly what Model Manager intends to change without changing
anything.

Example:

``` text
model-manager ensure pi-stack --dry-run -p
```

Conceptual result:

``` text
gpu3090:
  KEEP  nomic
  START qwen-coder using existing Ollama instance

dual2080:
  START reranker on port 8082

No changes performed.
```

### What it solves

The software can SSH into remote computers and launch/stop GPU
workloads. New users need confidence about what it is about to do.

### Why Phase One needs it

This is particularly valuable for a Marketplace plugin because users
will initially be running code they did not write against expensive GPU
resources.

### Suggested implementation phases

1.  Separate planning from execution internally.
2.  Produce an operation plan from the resolved configuration.
3.  Serialize that plan to JSON.
4.  Make normal execution consume the same plan.
5.  Add `--dry-run` to stop after planning.

This architecture also prepares the system for future scheduling.

------------------------------------------------------------------------

## 3.11 Orphan and State Reconciliation

### What it does

Detect differences between Model Manager's expected state and actual
machine state.

Examples: - Manager believes a model is running, but its process died. -
A previously launched process is still running after the manager
restarted. - A port is occupied by an unrelated process. - A service is
alive but fails inference verification.

### What it solves

Distributed process state inevitably drifts.

### Why Phase One needs it

A process manager that cannot recover from its own restart or a remote
crash will feel fragile.

### Suggested implementation phases

1.  Treat live machine inspection as authoritative for health.
2.  Reconcile configured services against actual processes/endpoints.
3.  Mark unknown/orphaned instances explicitly.
4.  Avoid killing unknown processes automatically.
5.  Provide an explicit cleanup/reconcile operation.

------------------------------------------------------------------------

## 3.12 Security and Secret Handling

### What it does

Establish safe defaults for remote execution and credentials.

At minimum: - Do not store SSH passwords/tokens directly in model
definitions. - Support SSH keys/agent-based authentication. - Allow
environment-variable or external-secret references. - Avoid exposing
secrets in logs or JSON responses. - Clearly document what remote
commands/scripts Model Manager installs and executes.

### What it solves

Model Manager has powerful remote execution capabilities. A convenient
but insecure default could become a serious problem once distributed
publicly.

### Why Phase One needs it

Security is difficult to retrofit after configuration formats and user
habits become established.

### Suggested implementation phases

1.  Define which values are allowed in configuration versus referenced
    externally.
2.  Redact sensitive values from logs/results.
3.  Document SSH assumptions and recommended key configuration.
4.  Validate remote helper-script integrity/version.
5.  Later: stronger host identity/policy and secrets integrations.

------------------------------------------------------------------------

# 4. Resource Reporting --- V1 Visibility, Later Scheduling

## What it does

Collect useful host capabilities and current resource information.

Possible fields: - OS. - Hostname. - GPU model(s). - Total VRAM. -
Available VRAM. - System RAM. - Running managed services. - Runtime
types. - Allocated ports.

## What it solves

Users and agents need to understand why a model can or cannot run on a
machine.

## Why it belongs in Phase One

Visibility is immediately useful for diagnostics even if Model Manager
does not yet make automatic placement decisions.

## Phase-One scope

Report resources. Do **not** build a full scheduler yet.

## Future evolution

Resource data can later support:

``` text
ensure qwen-coder
```

without specifying a machine. Model Manager could choose an appropriate
host based on model requirements and current resources.

------------------------------------------------------------------------

# 5. Features to Design For, But Not Block V1

## 5.1 Leases / Consumer Tracking

Track *who* currently needs a service separately from whether its
process is running.

Example:

``` text
pi-experiment -> qwen-coder
pi-experiment -> nomic-embed
editor        -> nomic-embed
```

Then:

``` text
release pi-experiment
```

could determine that Qwen is no longer needed while Nomic must remain
running because the editor still uses it.

This becomes increasingly important when runtimes such as Ollama host
several models or multiple applications share services.

### Recommendation

Design identifiers/state so leases can be added later, but do not delay
V1 unless current workflows already require them.

------------------------------------------------------------------------

## 5.2 Idle Shutdown

After leases/usage information exists, optionally stop expensive models
after an idle period.

Useful for scarce VRAM but potentially surprising if implemented too
early.

------------------------------------------------------------------------

## 5.3 Automatic Placement / Scheduling

Eventually allow Model Manager to choose the machine.

Inputs could include: - Required VRAM. - Available VRAM. - Runtime
support. - GPU architecture. - Model affinity/pinning. - Current load. -
Network locality.

This is powerful but substantially increases complexity. Keep it out of
the first Marketplace release.

------------------------------------------------------------------------

## 5.4 Fallback Models

Definitions could eventually express:

``` text
preferred: large-coder
fallback: small-coder
```

If the preferred model cannot be placed, Model Manager could return or
launch a compatible alternative.

Useful for agent workflows, but it requires clear capability semantics
and should follow a stable V1 contract.

------------------------------------------------------------------------

## 5.5 Priority and Preemption

A high-priority workload could eventually evict an idle or
lower-priority model to free VRAM.

This is scheduler territory and should be a later feature.

------------------------------------------------------------------------

## 5.6 Tailscale-Aware Endpoint Selection

For remote hosts, Model Manager could choose an address appropriate to
the caller---local LAN, hostname, or Tailscale address.

Useful for distributed setups, but endpoint correctness should come
first.

------------------------------------------------------------------------

# 6. Recommended V1 Implementation Order

The implementation order should reduce architectural rework.

### Step 1 --- Formalize the contract

Define schemas for: - Definitions. - Successful responses. - Errors. -
Service endpoints. - Group results. - Runtime/instance identity.

Do this first because nearly every other feature depends on stable
concepts.

### Step 2 --- Separate planning from execution

Resolve an `ensure` request into an explicit operation plan before
performing changes.

This enables: - Dry run. - Validation. - Dependency resolution. - Port
conflict detection. - Better logging. - Easier testing.

### Step 3 --- Add structured errors

Create explicit stages and stable error codes.

### Step 4 --- Harden instance/port handling

Represent runtimes and instances cleanly and guarantee returned
endpoints.

### Step 5 --- Add concurrency protection

Make ensure idempotent and prevent duplicate/racing starts.

### Step 6 --- Add dependencies and group semantics

Resolve dependency trees and make partial failure behavior explicit.

### Step 7 --- Add diagnostics

Implement status/doctor, resource reporting, logs, and reconciliation.

### Step 8 --- Security pass

Audit SSH execution, Windows helper deployment, secrets, logging,
permissions, and cleanup behavior.

### Step 9 --- Failure testing

Test ugly real-world conditions rather than only successful startup.

------------------------------------------------------------------------

# 7. Pre-Release Failure Test Matrix

Before Marketplace publication, deliberately test:

-   Remote machine powered off before `ensure`.
-   Remote machine disappears during startup.
-   SSH authentication fails.
-   Runtime executable is missing.
-   Runtime command exits immediately.
-   Configured port is already occupied.
-   Model exceeds available VRAM.
-   Process starts but HTTP server never becomes ready.
-   HTTP server responds but inference verification fails.
-   One member of a three-model group fails.
-   Manager REST service restarts while models remain running.
-   Remote machine restarts while manager remains running.
-   Two clients call `ensure` simultaneously.
-   Client calls `stop` while another client calls `ensure`.
-   Same runtime serves multiple models.
-   Two runtime instances exist on one host.
-   Dependency cycle exists in configuration.
-   Invalid group references a nonexistent model.
-   Log file becomes very large.
-   Windows helper script is missing/outdated.
-   Verification request hangs.
-   An unrelated process owns the desired port.
-   Model Manager discovers a process it did not start.

One particularly valuable test:

> Start a three-machine group and physically disconnect or shut down one
> machine during `ensure`.

The resulting behavior should be understandable entirely from the
returned JSON.

------------------------------------------------------------------------

# 8. V1 Definition of Done

A first Marketplace release is ready when a new user can:

1.  Install Model Manager using documented instructions.
2.  Define multiple Linux and/or Windows hosts.
3.  Define models with heterogeneous runtimes.
4.  Define a multi-host model group.
5.  Validate the configuration.
6.  Dry-run the group startup.
7.  Start/ensure the group with one command.
8.  Have Model Manager wait for real inference verification.
9.  Receive stable JSON containing usable endpoints.
10. Pretty-print the same response for human use.
11. Determine exactly why a model failed.
12. Inspect status and logs without manually SSHing into each machine.
13. Safely repeat `ensure` without duplicate processes.
14. Survive concurrent callers.
15. Restart the manager and reconcile existing remote services.
16. Stop/uninstall cleanly without damaging unrelated processes or
    configuration.

------------------------------------------------------------------------

# 9. Marketplace Positioning

Avoid positioning Model Manager as another model downloader, chat UI, or
inference engine.

A stronger description is:

> **Model Manager is a lightweight distributed orchestrator for local AI
> inference. Define models and model groups across your machines, bring
> an entire AI stack online with one command, verify each service with
> real inference, and return ready-to-use endpoints to humans, scripts,
> and coding agents.**

Short version:

> **Declare the AI services you need. Model Manager brings them online,
> verifies them, and tells your application where they are.**

### Key differentiators

-   Multi-machine.
-   Linux + Windows.
-   Runtime-agnostic.
-   Declarative configuration.
-   Model groups/stacks.
-   Real inference readiness verification.
-   JSON-first interface.
-   Designed for applications and coding agents, not only humans.
-   Central orchestration with simple SSH-based workers rather than
    requiring a heavyweight cluster platform.

------------------------------------------------------------------------

# 10. Scope Guardrail

Do not make V1 into a general-purpose cluster scheduler.

The first release should prove one thing exceptionally well:

> A user or agent can request a local AI model stack and trust Model
> Manager to bring the required services online across available
> machines, verify them, and return accurate endpoints.

Automatic placement, VRAM scheduling, leases, preemption, fallback
models, idle eviction, load balancing, and sophisticated networking can
grow naturally from that foundation after real users reveal which ones
matter most.
