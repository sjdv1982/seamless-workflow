"""Workflow-layer exceptions."""


class WorkflowError(Exception):
    """Base class for workflow Context errors."""


class AuthorityError(WorkflowError):
    """Raised when a dependent path is assigned as a local producer."""


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


class WorkflowExecutionError(RuntimeError):
    """Worker diagnostic text with a stable identity across detached snapshots.

    The message is the substrate's formatted diagnostic, not the original
    exception's args. No arbitrary worker exception class is reconstructed.
    """

    def __init__(self, message, *, failure_id=None):
        from uuid import uuid4
        super().__init__(message)
        self.failure_id = failure_id or uuid4().hex

    def __deepcopy__(self, memo):
        return type(self)(str(self), failure_id=self.failure_id)
