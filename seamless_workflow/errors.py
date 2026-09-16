"""Workflow-layer exceptions."""


from seamless.cell_errors import WorkflowError, AuthorityError


class DependencyError(WorkflowError):
    """Raised for illegal workflow dependency declarations."""


class PathError(WorkflowError):
    """Raised for invalid public or owner-local paths."""


class NodeError(WorkflowError):
    """Raised for invalid node operations."""


class ReadOnlyEndpointError(WorkflowError):
    """Raised when a producer operation targets a transformer result."""


class StaleWorkflowHandleError(WorkflowError):
    """Raised when a bound builder outlives its Context node."""


class ValueUnavailableError(WorkflowError):
    """Raised when a value update cannot materialize its current root value."""


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


from seamless.error_envelope import WorkflowExecutionError, execution_error
