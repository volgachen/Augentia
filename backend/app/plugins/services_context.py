"""Context variables set while plugin service code runs.

Kept in its own module so context.py (logger) and services wrappers in
host.py can share them without an import cycle.
"""

from contextvars import ContextVar

# Run id of the service run whose code is executing (None outside services).
current_run_id: ContextVar[str | None] = ContextVar("augentia_plugin_run_id", default=None)
# (plugin_id, service, session_id) of the executing service, for self-stop detection.
current_service: ContextVar[tuple[str, str, str | None] | None] = ContextVar(
    "augentia_plugin_service", default=None)
