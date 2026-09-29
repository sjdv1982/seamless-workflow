"""Workflow-layer exceptions."""


from seamless.cell_errors import WorkflowError, AuthorityError, ValueUnavailableError


class DependencyError(TypeError, WorkflowError):
    """Raised for illegal workflow dependency declarations."""


class PathError(WorkflowError):
    """Raised for invalid public or owner-local paths."""


class NodeError(WorkflowError):
    """Raised for invalid node operations."""


class ReadOnlyEndpointError(WorkflowError):
    """Raised when a producer operation targets a transformer result."""


class StaleWorkflowHandleError(WorkflowError):
    """Raised when a bound builder outlives its Context node."""


__all__ = [
    "WorkflowError",
    "AuthorityError",
    "DependencyError",
    "PathError",
    "NodeError",
    "ReadOnlyEndpointError",
    "StaleWorkflowHandleError",
    "ValueUnavailableError",
]

class ClosedContextError(RuntimeError):
    """Operation on a closed Context."""


class ReentrantContextError(RuntimeError):
    """Public ingress was called from a controller turn."""


class ConcurrentUpdateError(RuntimeError):
    """An optimistic value edit exhausted its retry budget."""


class ControllerFailedError(RuntimeError):
    """An internal continuation failed; this Context must be closed."""


from seamless.error_envelope import (
    WorkflowExecutionError,
    execution_error as _execution_error,
)


def execution_error(exc):
    """Normalize execution errors without adding a type prefix to their message."""
    error = _execution_error(exc)
    if isinstance(error, WorkflowExecutionError) and error is not exc:
        return WorkflowExecutionError(
            str(error),
            failure_id=error.failure_id,
            kind=error.kind,
        )
    return error
