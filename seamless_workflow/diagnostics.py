"""Opt-in, bounded snapshots at accepted controller-turn boundaries.

record_turns(ctx) records immutable checksum/state data, without materializing
values. Message classes follow the five classes in the controller design.
"""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class NodeSnapshot:
    path: tuple
    state: str
    checksum: str | None


@dataclass(frozen=True)
class TurnSnapshot:
    sequence: int
    message_class: int
    nodes: tuple[NodeSnapshot, ...]


class TurnLog:
    def __init__(self, limit):
        self._lock = Lock()
        self._entries = deque(maxlen=limit)

    def _append(self, entry):
        with self._lock:
            self._entries.append(entry)

    def entries(self):
        """Return a consistent immutable snapshot of the bounded log."""
        with self._lock:
            return tuple(self._entries)


@contextmanager
def record_turns(context, *, limit=1024):
    """Record turns accepted between entry and exit; nested recorders coexist."""
    from .errors import ClosedContextError, ControllerFailedError
    if not isinstance(limit, int) or limit < 1:
        raise ValueError('limit must be a positive integer')
    log = TurnLog(limit)
    context._controller.call('_register_turn_log', log, klass=1)
    try:
        yield log
    finally:
        try:
            context._controller.call('_unregister_turn_log', log, klass=1)
        except (ClosedContextError, ControllerFailedError):
            pass


@contextmanager
def record_attachments(context, *, limit=1024):
    """Record immutable classification, delivery and detector events."""
    from .errors import ClosedContextError, ControllerFailedError
    if not isinstance(limit, int) or limit < 1:
        raise ValueError('limit must be a positive integer')
    log = TurnLog(limit)
    context._controller.call('_mount_register_log', log, klass=1)
    try: yield log
    finally:
        try: context._controller.call('_mount_unregister_log', log, klass=1)
        except (ClosedContextError, ControllerFailedError): pass
