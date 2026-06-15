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


__all__ = [
    "WorkflowError",
    "AuthorityError",
    "DependencyError",
    "PathError",
    "NodeError",
]
