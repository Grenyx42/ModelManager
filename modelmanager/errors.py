from __future__ import annotations


class ModelManagerError(Exception):
    """Base for every error this coordinator raises on purpose. Carries a
    status_code (for the FastAPI response) and a stable code string (for a
    human or coding agent to branch on without string-matching the message)."""

    status_code = 500
    code = "INTERNAL_ERROR"


class NotFoundError(ModelManagerError):
    status_code = 404
    code = "NOT_FOUND"


class ForbiddenError(ModelManagerError):
    """The caller authenticated with a valid token, but it's a scoped grant
    (see modelmanager/grants.py) that doesn't cover whatever system(s) or
    operation this request needs - a granted token writing anything, or
    touching a model/system outside its allowed_systems."""

    status_code = 403
    code = "FORBIDDEN"


class ConflictError(ModelManagerError):
    status_code = 409
    code = "CONFLICT"


class AmbiguousNameError(ConflictError):
    """A partial model name (see Manager.resolve_name) matched more than one
    definition - the caller needs to be more specific."""

    code = "AMBIGUOUS_NAME"


class GpuGroupBusyError(ConflictError):
    """ensure() couldn't free the gpu_group a candidate needs - it's occupied
    by a pinned model, a model with an active lease, or one that hasn't been
    idle long enough yet. Same actionable response in all three cases: wait,
    or stop the occupant yourself."""

    code = "GPU_GROUP_BUSY"


class LeaseConflictError(ConflictError):
    """stop() was called on a model with active leases, without force=true."""

    code = "LEASE_CONFLICT"


class PortInUseError(ConflictError):
    """A pre-flight check found something already listening on the port a
    candidate is about to bind, before launch was even attempted."""

    code = "PORT_CONFLICT"


class RegistryValidationError(ModelManagerError):
    """A proposed registry write failed validation; nothing was changed."""

    status_code = 422
    code = "VALIDATION_ERROR"


class ReloadUnavailableError(ModelManagerError):
    """This Manager has no registry_path, so there is nothing to reload from."""

    status_code = 400
    code = "RELOAD_UNAVAILABLE"


class EngineControlError(ModelManagerError):
    """An adapter's start()/stop() actually tried to control the engine and
    failed: the process crashed on startup, a remote invoke failed, or the
    engine itself rejected the request (e.g. Ollama's "model not found")."""

    status_code = 502
    code = "ENGINE_CONTROL_FAILED"


class HostUnreachableError(ModelManagerError):
    """SSH itself couldn't connect (host down, DNS failure, auth failure) -
    distinct from the remote command running and failing on its own."""

    status_code = 502
    code = "HOST_UNREACHABLE"


class CommandTimeoutError(ModelManagerError):
    """A local or SSH'd command exceeded its timeout and was killed."""

    status_code = 502
    code = "COMMAND_TIMEOUT"


class CommandFailedError(ModelManagerError):
    """A local or SSH'd command actually ran (the connection itself was fine)
    but exited nonzero or produced output that couldn't be used - nvidia-smi
    missing, a remote mkdir/upload rejected, a Windows home directory that
    couldn't be resolved. Distinct from HostUnreachableError (the connection
    itself failed) and EngineControlError (specifically starting/stopping a
    model's engine)."""

    status_code = 502
    code = "COMMAND_FAILED"
