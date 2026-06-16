"""Transient runtime scheduling records for workflow Context."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Any, Deque, Literal

from seamless import Checksum

from .graph import NodePath

RunPhase = Literal["running", "superseded", "cancelled", "completed"]


@dataclass
class ExceptionInfo:
    type: str
    message: str

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ExceptionInfo":
        return cls(type=type(exc).__name__, message=str(exc))


@dataclass
class RunRecord:
    node_path: NodePath
    et: Any
    identity_checksum: Checksum | None
    result_checksum: Checksum | None
    exception: ExceptionInfo | None = None
    phase: RunPhase = "completed"
    generation: int = 0
    hold_deadline: float | None = None
    hold_kind: str | None = None


@dataclass
class Scheduler:
    superseded_cap: int = 3
    self_edit_hold_seconds: float = 15.0

    def hold_deadline(self, hold_kind: str | None = "self-edit") -> float | None:
        if hold_kind == "self-edit":
            return time() + self.self_edit_hold_seconds
        return None


@dataclass
class ContextRuntime:
    scheduler: Scheduler = field(default_factory=Scheduler)
    current_runs: dict[NodePath, RunRecord] = field(default_factory=dict)
    superseded_runs: dict[NodePath, Deque[RunRecord]] = field(default_factory=dict)
    generation: int = 0

    def next_generation(self) -> int:
        self.generation += 1
        return self.generation

    def supersede(self, path: NodePath, hold_kind: str = "self-edit") -> None:
        current = self.current_runs.pop(path, None)
        if current is None:
            return
        current.phase = "superseded"
        current.hold_kind = hold_kind
        current.hold_deadline = self.scheduler.hold_deadline(hold_kind)
        queue = self.superseded_runs.setdefault(path, deque())
        queue.append(current)
        while len(queue) > self.scheduler.superseded_cap:
            evicted = queue.popleft()
            evicted.phase = "cancelled"

    def prune(self, paths: set[NodePath] | None = None) -> int:
        pruned = 0
        for path in list(self.superseded_runs):
            if paths is not None and path not in paths:
                continue
            queue = self.superseded_runs[path]
            while queue:
                record = queue.popleft()
                if record.phase != "cancelled":
                    record.phase = "cancelled"
                    pruned += 1
            del self.superseded_runs[path]
        return pruned


__all__ = [
    "ContextRuntime",
    "ExceptionInfo",
    "RunRecord",
    "RunPhase",
    "Scheduler",
]
