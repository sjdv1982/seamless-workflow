"""Durable graph structures for workflow Context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from seamless import Checksum

NodeKind = Literal["cell", "transformer"]
NodeState = Literal["unwired", "blocked", "waiting", "computing", "complete", "failed"]
BlockReason = Literal["blocked-by-unwired", "blocked-by-error"]
NodePath = tuple[str, ...]
Path = tuple[Any, ...]
ViewPath = tuple[Any, ...]


@dataclass
class ConstantProducer:
    checksum: Checksum
    celltype: str = "mixed"


@dataclass
class CellConfig:
    celltype: str = "mixed"
    target_celltype: str = "mixed"
    validator: Any = None
    validator_language: str | None = None


@dataclass
class TransformerConfig:
    code: Any = None
    code_checksum: Checksum | None = None
    language: str = "python"
    callable: Any = None
    pins: set[str] = field(default_factory=set)
    celltypes: dict[str, str] = field(default_factory=lambda: {"result": "mixed"})
    optional_pins: set[str] = field(default_factory=set)
    modules: dict[str, Any] = field(default_factory=dict)
    globals: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    environment: Any = None
    scratch: bool = False
    local: bool | None = None
    direct_print: bool = False
    call_mode: Literal["delayed", "direct"] = "delayed"


@dataclass
class Node:
    kind: NodeKind
    state: NodeState = "unwired"
    block_reason: BlockReason | None = None
    cell_config: CellConfig | None = None
    transformer_config: TransformerConfig | None = None
    cell_root_producer: ConstantProducer | None = None
    transformer_pin_producers: dict[str, ConstantProducer] = field(default_factory=dict)
    current_checksum: Checksum | None = None
    active_count: int = 0
    derived_active_count: int = 0
    exception: BaseException | None = None


@dataclass(frozen=True)
class Edge:
    source: ViewPath
    target: ViewPath
    source_celltype: str = "mixed"
    target_celltype: str = "mixed"


class ContextGraph:
    def __init__(self) -> None:
        self.nodes: dict[NodePath, Node] = {}
        self.edges: list[Edge] = []
        self.incoming: dict[NodePath, set[int]] = {}
        self.outgoing: dict[NodePath, set[int]] = {}
        self.namespaces: set[NodePath] = set()

    def rebuild_indexes(self) -> None:
        self.incoming = {path: set() for path in self.nodes}
        self.outgoing = {path: set() for path in self.nodes}
        for index, edge in enumerate(self.edges):
            source_node, _ = self.resolve_existing(edge.source)
            target_node, _ = self.resolve_existing(edge.target)
            self.outgoing.setdefault(source_node, set()).add(index)
            self.incoming.setdefault(target_node, set()).add(index)

    def resolve_existing(self, view_path: ViewPath) -> tuple[NodePath, Path]:
        for size in range(len(view_path), 0, -1):
            node_path = tuple(view_path[:size])
            if node_path in self.nodes:
                return node_path, tuple(view_path[size:])
        raise KeyError(view_path)

    def has_prefix(self, prefix: NodePath) -> bool:
        return any(path[: len(prefix)] == prefix for path in self.nodes) or prefix in self.namespaces

    def descendants(self, prefix: NodePath) -> list[NodePath]:
        return sorted(path for path in self.nodes if path[: len(prefix)] == prefix)

    def first_free(self, stem: str) -> NodePath:
        index = 1
        while (f"{stem}{index}",) in self.nodes or (f"{stem}{index}",) in self.namespaces:
            index += 1
        return (f"{stem}{index}",)


__all__ = [
    "BlockReason",
    "CellConfig",
    "ConstantProducer",
    "ContextGraph",
    "Edge",
    "Node",
    "NodeKind",
    "NodePath",
    "NodeState",
    "Path",
    "TransformerConfig",
    "ViewPath",
]
