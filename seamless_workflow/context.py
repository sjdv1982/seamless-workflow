"""Reactive workflow Context."""

from __future__ import annotations

import copy
import inspect
from hashlib import sha256
from types import FunctionType
from typing import Any
from uuid import uuid4

from seamless import Buffer, Cell, Checksum

from .adapters import buffer_for_checksum, checksum_for_value, normalize_checksum, value_for_checksum
from .builder_state import BoundCellBackend, BoundTransformerBackend
from .errors import AuthorityError, DependencyError, NodeError, PathError
from .graph import (
    CellConfig,
    ConstantProducer,
    ContextGraph,
    Edge,
    Node,
    NodePath,
    Overlay,
    TransformerConfig,
)
from .scheduler import ContextRuntime, ExceptionInfo, RunRecord
from .views import CellView, MissingView, SubContextView, TransformerResultView, TransformerView

PIN_CELLTYPES = {"plain", "mixed", "deepcell", "deepfolder", "folder"}


class Context:
    """Top-level workflow graph owner or subcontext namespace builder."""

    def __init__(self, eager: bool = True) -> None:
        object.__setattr__(self, "top_id", uuid4().hex)
        object.__setattr__(self, "_graph", ContextGraph())
        object.__setattr__(self, "_runtime", ContextRuntime())
        object.__setattr__(self, "eager", eager)
        object.__setattr__(self, "_prefix", ())

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._view(self._prefix + (name,))

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") or name in {"top_id", "eager"}:
            object.__setattr__(self, name, value)
            return
        self._assign(self._prefix + (name,), value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            object.__delattr__(self, name)
            return
        self._delete(self._prefix + (name,))

    def __getitem__(self, key: str):
        return self._view(self._prefix + (str(key),))

    def __setitem__(self, key: str, value: Any) -> None:
        self._assign(self._prefix + (str(key),), value)

    def __delitem__(self, key: str) -> None:
        self._delete(self._prefix + (str(key),))

    def _view(self, path: tuple[str, ...]):
        if path in self._graph.nodes:
            node = self._graph.nodes[path]
            if node.kind == "cell":
                return CellView(self, path)
            return TransformerView(self, path)
        if path in self._graph.namespaces or self._graph.has_prefix(path):
            return SubContextView(self, path)
        return MissingView(self, path)

    def _assign(self, path: tuple[str, ...], value: Any) -> None:
        if value is None:
            self._delete(path)
            return
        from seamless_transformer.transformer_class import Transformer

        if path in self._graph.nodes:
            node = self._graph.nodes[path]
            if node.kind == "cell":
                if self._is_bound_source(value):
                    self._replace_edges_to(path)
                    self._add_edge(self._source_path(value), path)
                elif isinstance(value, Cell):
                    if value._workflow_backend is not None and value._workflow_backend.context is self:
                        self._replace_edges_to(path)
                        self._add_edge(value._workflow_backend.node_path + value._workflow_backend.owner_path, path)
                    else:
                        self._replace_cell_from_builder(path, value)
                elif callable(value):
                    raise NodeError("Cannot replace a cell node with transformer code")
                else:
                    self._set_cell_value(path, (), value)
            else:
                if isinstance(value, Transformer):
                    self._replace_transformer_from_builder(path, value)
                elif callable(value) or isinstance(value, str):
                    self._set_transformer_code(path, value)
                else:
                    raise NodeError("Cannot replace a transformer node with a non-transformer value")
            self._derive_all()
            return

        if isinstance(value, Context):
            self._graph.namespaces.add(path)
            self._derive_all()
            return
        if isinstance(value, SubContextView):
            self._copy_subcontext(value._prefix, path)
            self._derive_all()
            return
        if self._is_bound_source(value):
            self._create_cell(path)
            self._add_edge(self._source_path(value), path)
            self._derive_all()
            return
        if isinstance(value, Cell):
            self._create_cell_from_builder(path, value)
            self._derive_all()
            return
        if isinstance(value, Transformer):
            self._create_transformer_from_builder(path, value)
            self._derive_all()
            return
        if callable(value):
            self._create_transformer(path, value)
            self._derive_all()
            return
        self._create_cell(path)
        self._set_cell_value(path, (), value)
        self._derive_all()

    def _delete(self, path: tuple[str, ...]) -> None:
        removed = False
        if path in self._graph.nodes:
            for node_path in self._graph.descendants(path):
                self._graph.nodes.pop(node_path, None)
                self._runtime.current_runs.pop(node_path, None)
                self._runtime.superseded_runs.pop(node_path, None)
            removed = True
        if path in self._graph.namespaces:
            self._graph.namespaces = {p for p in self._graph.namespaces if p[: len(path)] != path}
            removed = True
        if not removed:
            try:
                node_path, local = self._graph.resolve_existing(path)
            except KeyError:
                return
            node = self._graph.nodes[node_path]
            if node.kind == "cell":
                self._delete_cell_path(node_path, local)
            else:
                self._delete_transformer_pin(node_path, local)
            return
        self._graph.edges = [
            edge
            for edge in self._graph.edges
            if edge.source[: len(path)] != path and edge.target[: len(path)] != path
        ]
        self._derive_all()

    def _create_cell(self, path: NodePath, *, celltype: str = "mixed") -> Node:
        if path in self._graph.nodes:
            raise NodeError(path)
        node = Node(
            kind="cell",
            cell_config=CellConfig(celltype=celltype, target_celltype=celltype),
            cell_overlay=Overlay(),
        )
        self._graph.nodes[path] = node
        return node

    def _create_cell_from_builder(self, path: NodePath, cell: Cell) -> None:
        self._create_cell(path, celltype=cell.celltype)
        self._replace_cell_from_builder(path, cell)

    def _replace_cell_from_builder(self, path: NodePath, cell: Cell) -> None:
        node = self._graph.nodes[path]
        if node.kind != "cell":
            raise NodeError(path)
        node.cell_config = CellConfig(
            celltype=cell.celltype,
            target_celltype=cell.target_celltype,
            validator=cell.validator,
            validator_language=cell.validator_language,
        )
        node.cell_overlay = Overlay()
        input_ref = cell.input_ref
        if isinstance(input_ref, Cell):
            if input_ref._workflow_backend is not None:
                if input_ref._workflow_backend.context is not self:
                    raise DependencyError("Cross-top-level dependencies are not supported")
                self._add_edge(
                    input_ref._workflow_backend.node_path + input_ref._workflow_backend.owner_path,
                    path,
                )
            else:
                upstream_path = self._graph.first_free("cell")
                self._create_cell_from_builder(upstream_path, input_ref)
                self._add_edge(upstream_path, path)
        elif self._is_bound_source(input_ref):
            self._add_edge(self._source_path(input_ref), path)
        elif input_ref is not None:
            self._set_cell_value(path, (), input_ref)
        cell._workflow_backend = BoundCellBackend(self, path)

    def _create_transformer(self, path: NodePath, code: Any) -> None:
        cfg = self._transformer_config_from_code(code)
        self._graph.nodes[path] = Node(
            kind="transformer",
            transformer_config=cfg,
            transformer_pin_overlays={"code": Overlay({(): ConstantProducer(cfg.code_checksum, "python" if cfg.language == "python" else "text")})},
        )

    def _create_transformer_from_builder(self, path: NodePath, transformer) -> None:
        self._graph.nodes[path] = Node(
            kind="transformer",
            transformer_config=self._transformer_config_from_builder(transformer),
            transformer_pin_overlays={},
        )
        cfg = self._graph.nodes[path].transformer_config
        if cfg.code_checksum is not None:
            self._graph.nodes[path].transformer_pin_overlays["code"] = Overlay(
                {(): ConstantProducer(cfg.code_checksum, "python" if cfg.language == "python" else "text")}
            )
        for pin, value in transformer._args.items():
            self._set_transformer_pin(path, (pin,), value)
        transformer._workflow_backend = BoundTransformerBackend(self, path)

    def _replace_transformer_from_builder(self, path: NodePath, transformer) -> None:
        old = self._graph.nodes[path].transformer_config
        new = self._transformer_config_from_builder(transformer)
        new.pins.update(old.pins)
        self._graph.nodes[path].transformer_config = new
        transformer._workflow_backend = BoundTransformerBackend(self, path)

    def _transformer_config_from_code(self, code: Any) -> TransformerConfig:
        cfg = TransformerConfig(code=code, callable=code if callable(code) else None)
        if callable(code):
            cfg.language = "python"
            source = inspect.getsource(code)
            cfg.code_checksum = Buffer(source, "python").get_checksum()
            cfg.pins = set(inspect.signature(code).parameters)
            cfg.celltypes = {pin: "mixed" for pin in cfg.pins}
            cfg.celltypes["result"] = "mixed"
        else:
            cfg.language = "text"
            cfg.code_checksum = Buffer(str(code), "text").get_checksum()
        return cfg

    def _transformer_config_from_builder(self, transformer) -> TransformerConfig:
        cfg = TransformerConfig()
        cfg.language = transformer.language
        cfg.code = transformer.code
        cfg.code_checksum = transformer.code.get_checksum()
        cfg.callable = getattr(transformer, "_workflow_callable", None)
        cfg.pins = {k for k in transformer._celltypes if k != "result"}
        cfg.celltypes = dict(transformer._celltypes)
        cfg.optional_pins = set(transformer.optional_pins)
        cfg.modules = copy.deepcopy(transformer._modules)
        cfg.globals = copy.deepcopy(transformer._globals)
        cfg.meta = copy.deepcopy(transformer.meta)
        cfg.environment = transformer.environment
        cfg.scratch = transformer.scratch
        cfg.local = transformer.local
        cfg.direct_print = transformer.direct_print
        for pin, value in transformer._args.items():
            cfg.pins.add(pin)
        return cfg

    def _set_cell_config(self, path: NodePath, **updates) -> None:
        cfg = self._graph.nodes[path].cell_config
        for key, value in updates.items():
            if key == "target_celltype" and value is None:
                value = cfg.celltype
            setattr(cfg, key, value)
        self._derive_all()

    def _set_cell_value(self, node_path: NodePath, local: tuple[str, ...], value: Any) -> None:
        celltype = self._graph.nodes[node_path].cell_config.celltype
        checksum = checksum_for_value(value, celltype)
        self._set_cell_checksum(node_path, local, checksum, celltype=celltype)

    def _set_cell_values(self, node_path: NodePath, values: dict[tuple[str, ...], Any]) -> None:
        for local, value in values.items():
            self._validate_cell_local(node_path, local)
            self._check_authority(node_path, local)
        for local, value in values.items():
            celltype = self._graph.nodes[node_path].cell_config.celltype
            checksum = checksum_for_value(value, celltype)
            self._store_cell_producer(node_path, local, checksum, celltype)
        self._derive_all()

    def _set_cell_checksum(self, node_path: NodePath, local: tuple[str, ...], checksum, *, celltype: str | None = None) -> None:
        self._validate_cell_local(node_path, local)
        self._check_authority(node_path, local)
        if celltype is None:
            celltype = self._graph.nodes[node_path].cell_config.celltype
        self._store_cell_producer(node_path, local, normalize_checksum(checksum), celltype)
        self._derive_all()

    def _store_cell_producer(self, node_path, local, checksum, celltype):
        overlay = self._graph.nodes[node_path].cell_overlay
        if local == ():
            overlay.entries = {(): ConstantProducer(checksum, celltype)}
            self._remove_edges_targeting(node_path, (), descendants=True)
        else:
            overlay.entries.pop(local, None)
            overlay.entries.pop((), None)
            overlay.entries[local] = ConstantProducer(checksum, celltype)
            self._remove_edges_targeting(node_path, local, descendants=False)

    def _validate_cell_local(self, node_path: NodePath, local: tuple[str, ...]) -> None:
        if len(local) > 1:
            raise PathError("Cell subcell paths are limited to one level")
        node = self._graph.nodes[node_path]
        if local and node.cell_config.celltype not in PIN_CELLTYPES:
            raise PathError(f"Celltype {node.cell_config.celltype!r} does not support Cell.pins")

    def _delete_cell_path(self, node_path: NodePath, local: tuple[str, ...]) -> None:
        self._validate_cell_local(node_path, local)
        node = self._graph.nodes[node_path]
        node.cell_overlay.entries.pop(local, None)
        self._remove_edges_targeting(node_path, local, descendants=True)
        self._derive_all()

    def _set_transformer_code(self, node_path: NodePath, code: Any) -> None:
        cfg = self._graph.nodes[node_path].transformer_config
        new = self._transformer_config_from_code(code)
        cfg.code = code
        cfg.callable = new.callable
        cfg.code_checksum = new.code_checksum
        cfg.language = new.language
        cfg.pins.update(new.pins)
        cfg.celltypes.update(new.celltypes)
        self._set_transformer_pin_checksum(node_path, ("code",), new.code_checksum, celltype="python" if new.language == "python" else "text")

    def _set_transformer_pin(self, node_path: NodePath, local: tuple[str, ...], value: Any) -> None:
        cfg = self._graph.nodes[node_path].transformer_config
        if not local:
            raise PathError("Transformer pin path may not be empty")
        cfg.pins.add(local[0])
        cfg.celltypes.setdefault(local[0], "mixed")
        checksum = checksum_for_value(value, cfg.celltypes.get(local[0], "mixed"))
        self._set_transformer_pin_checksum(node_path, local, checksum, celltype=cfg.celltypes.get(local[0], "mixed"))

    def _set_transformer_pin_checksum(self, node_path: NodePath, local: tuple[str, ...], checksum, *, celltype: str = "mixed") -> None:
        self._check_authority(node_path, local)
        pin = local[0]
        overlay = self._graph.nodes[node_path].transformer_pin_overlays.setdefault(pin, Overlay())
        overlay.entries[tuple(local[1:])] = ConstantProducer(normalize_checksum(checksum), celltype)
        self._remove_edges_targeting(node_path, local, descendants=False)
        self._derive_all()

    def _delete_transformer_pin(self, node_path: NodePath, local: tuple[str, ...]) -> None:
        if not local:
            raise PathError("Cannot delete all transformer pins at once")
        pin = local[0]
        overlay = self._graph.nodes[node_path].transformer_pin_overlays.get(pin)
        if overlay is not None:
            sub = tuple(local[1:])
            for key in list(overlay.entries):
                if key[: len(sub)] == sub:
                    del overlay.entries[key]
        self._remove_edges_targeting(node_path, local, descendants=True)
        self._derive_all()

    def _is_bound_source(self, value: Any) -> bool:
        return isinstance(value, (CellView, TransformerView, TransformerResultView)) or (
            isinstance(value, Cell) and value._workflow_backend is not None
        )

    def _source_path(self, value: Any) -> tuple[str, ...]:
        if isinstance(value, CellView):
            if value._context is not self:
                raise DependencyError("Cross-top-level dependencies are not supported")
            return value._node_path
        if isinstance(value, TransformerResultView):
            if value._context is not self:
                raise DependencyError("Cross-top-level dependencies are not supported")
            return value._node_path
        if isinstance(value, TransformerView):
            if value._context is not self:
                raise DependencyError("Cross-top-level dependencies are not supported")
            return value._node_path
        if isinstance(value, Cell) and value._workflow_backend is not None:
            if value._workflow_backend.context is not self:
                raise DependencyError("Cross-top-level dependencies are not supported")
            return value._workflow_backend.node_path + value._workflow_backend.owner_path
        raise DependencyError(value)

    def _add_edge(self, source: tuple[str, ...], target: tuple[str, ...]) -> None:
        source_node, _ = self._graph.resolve_existing(source)
        target_node, target_local = self._graph.resolve_existing(target)
        if source_node == target_node:
            raise DependencyError("Self-dependencies are not supported")
        self._check_no_cycle(source_node, target_node)
        self._check_authority(target_node, target_local)
        self._graph.edges.append(Edge(source=source, target=target))
        self._clear_local_target(target_node, target_local)
        self._derive_all()

    def _check_no_cycle(self, source: NodePath, target: NodePath) -> None:
        seen = set()
        stack = [source]
        while stack:
            node = stack.pop()
            if node == target:
                raise DependencyError("Dependency cycle")
            if node in seen:
                continue
            seen.add(node)
            for edge in self._graph.edges:
                try:
                    edge_source, _ = self._graph.resolve_existing(edge.source)
                    edge_target, _ = self._graph.resolve_existing(edge.target)
                except KeyError:
                    continue
                if edge_source == node:
                    stack.append(edge_target)

    def _check_authority(self, node_path: NodePath, local: tuple[str, ...]) -> None:
        for edge in self._graph.edges:
            try:
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                continue
            if target_node != node_path:
                continue
            if local == () or target_local == local or local[: len(target_local)] == target_local:
                raise AuthorityError(f"{node_path + local!r} is produced by an incoming edge")

    def _clear_local_target(self, node_path: NodePath, local: tuple[str, ...]) -> None:
        node = self._graph.nodes[node_path]
        if node.kind == "cell":
            if local == ():
                node.cell_overlay.entries.clear()
            else:
                node.cell_overlay.entries.pop((), None)
                node.cell_overlay.entries.pop(local, None)
        elif local:
            overlay = node.transformer_pin_overlays.get(local[0])
            if overlay is not None:
                overlay.entries.pop(tuple(local[1:]), None)

    def _replace_edges_to(self, node_path: NodePath) -> None:
        self._graph.edges = [
            edge
            for edge in self._graph.edges
            if self._graph.resolve_existing(edge.target)[0] != node_path
        ]

    def _remove_edges_targeting(self, node_path: NodePath, local: tuple[str, ...], *, descendants: bool) -> None:
        kept = []
        for edge in self._graph.edges:
            try:
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                continue
            if target_node != node_path:
                kept.append(edge)
                continue
            match = target_local == local
            if descendants:
                match = match or target_local[: len(local)] == local
            if not match:
                kept.append(edge)
        self._graph.edges = kept

    def _derive_all(self) -> None:
        self._graph.rebuild_indexes()
        for path in sorted(self._graph.nodes):
            self._derive_node(path)

    def _derive_node(self, path: NodePath) -> None:
        node = self._graph.nodes[path]
        old_identity = self._run_identity(path, node)
        node.exception = None
        if node.kind == "cell":
            self._derive_cell(path, node)
        else:
            self._derive_transformer(path, node)
        self._sync_run_record(path, old_identity, node)

    def _run_identity(self, path: NodePath, node: Node) -> str | None:
        checksum = node.current_checksum.hex() if node.current_checksum else ""
        return f"{node.kind}:{'.'.join(path)}:{node.state}:{checksum}"

    def _identity_checksum(self, identity: str | None) -> Checksum | None:
        if identity is None:
            return None
        return Checksum(sha256(identity.encode()).digest())

    def _sync_run_record(self, path: NodePath, old_identity: str | None, node: Node) -> None:
        new_identity = self._run_identity(path, node)
        current = self._runtime.current_runs.get(path)
        current_identity = (
            current.identity_checksum.hex() if current and current.identity_checksum else None
        )
        new_identity_checksum = self._identity_checksum(new_identity)
        new_identity_hex = new_identity_checksum.hex() if new_identity_checksum else None
        if current is not None and current_identity != new_identity_hex:
            self._runtime.supersede(path)
        if node.state in {"complete", "failed", "computing"}:
            exception = ExceptionInfo.from_exception(node.exception) if node.exception else None
            phase = "completed" if node.state in {"complete", "failed"} else "running"
            self._runtime.current_runs[path] = RunRecord(
                node_path=path,
                et=new_identity,
                identity_checksum=new_identity_checksum,
                result_checksum=node.current_checksum,
                exception=exception,
                phase=phase,
                generation=self._runtime.next_generation(),
            )
        else:
            self._runtime.current_runs.pop(path, None)

    def _derive_cell(self, path: NodePath, node: Node) -> None:
        overlay = node.cell_overlay.entries
        incoming = self._incoming_values(path)
        if () in overlay:
            node.current_checksum = overlay[()].checksum
            node.state = "complete"
            node.block_reason = None
            return
        if () in incoming:
            state, checksum = incoming[()]
            self._apply_upstream_state(node, state, checksum)
            return
        pieces = {}
        any_piece = False
        for local, producer in sorted(overlay.items()):
            if len(local) == 1:
                pieces[local[0]] = value_for_checksum(producer.checksum, producer.celltype)
                any_piece = True
        for local, (state, checksum) in sorted(incoming.items()):
            if len(local) != 1:
                continue
            any_piece = True
            if state != "complete":
                node.current_checksum = None
                node.state = "blocked" if state in {"blocked", "failed", "unwired"} else "waiting"
                node.block_reason = "blocked-by-error" if state == "failed" else "blocked-by-unwired"
                return
            pieces[local[0]] = value_for_checksum(checksum, "mixed")
        if any_piece:
            node.current_checksum = checksum_for_value(pieces, node.cell_config.celltype)
            node.state = "complete"
            node.block_reason = None
        else:
            node.current_checksum = None
            node.state = "unwired"
            node.block_reason = None

    def _derive_transformer(self, path: NodePath, node: Node) -> None:
        cfg = node.transformer_config
        if cfg.code_checksum is None:
            node.state = "unwired"
            node.current_checksum = None
            return
        kwargs = {}
        incoming = self._incoming_values(path)
        for pin in sorted(cfg.pins):
            checksum = self._assembled_pin_checksum(path, pin, incoming)
            if pin in cfg.optional_pins and checksum is None:
                continue
            if isinstance(checksum, tuple):
                state = checksum[0]
                node.state = "blocked" if state in {"blocked", "failed", "unwired"} else "waiting"
                node.block_reason = "blocked-by-error" if state == "failed" else "blocked-by-unwired"
                node.current_checksum = None
                return
            if checksum is None:
                node.state = "unwired"
                node.current_checksum = None
                node.block_reason = None
                return
            kwargs[pin] = value_for_checksum(checksum, cfg.celltypes.get(pin, "mixed"))
        if cfg.callable is None:
            node.state = "complete" if node.current_checksum is not None else "waiting"
            return
        if not self.eager and node.active_count == 0 and node.derived_active_count == 0:
            node.state = "waiting"
            return
        try:
            result = cfg.callable(**kwargs)
            node.current_checksum = checksum_for_value(result, cfg.celltypes.get("result", "mixed"))
            node.state = "complete"
            node.block_reason = None
        except BaseException as exc:
            node.exception = exc
            node.current_checksum = None
            node.state = "failed"
            node.block_reason = None

    def _assembled_pin_checksum(self, node_path: NodePath, pin: str, incoming):
        node = self._graph.nodes[node_path]
        cfg = node.transformer_config
        overlay = node.transformer_pin_overlays.get(pin)
        if overlay and () in overlay.entries:
            return overlay.entries[()].checksum
        if (pin,) in incoming:
            state, checksum = incoming[(pin,)]
            return checksum if state == "complete" else (state, None)
        pieces = {}
        has_piece = False
        if overlay:
            for local, producer in sorted(overlay.entries.items()):
                if not local:
                    continue
                has_piece = True
                cursor = pieces
                for part in local[:-1]:
                    cursor = cursor.setdefault(part, {})
                cursor[local[-1]] = value_for_checksum(producer.checksum, producer.celltype)
        for local, (state, checksum) in sorted(incoming.items()):
            if not local or local[0] != pin or len(local) == 1:
                continue
            has_piece = True
            if state != "complete":
                return (state, None)
            subpath = local[1:]
            cursor = pieces
            for part in subpath[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[subpath[-1]] = value_for_checksum(checksum, cfg.celltypes.get(pin, "mixed"))
        if not has_piece:
            return None
        return checksum_for_value(pieces, cfg.celltypes.get(pin, "mixed"))

    def _incoming_values(self, path: NodePath) -> dict[tuple[str, ...], tuple[str, Checksum | None]]:
        result = {}
        for edge in self._graph.edges:
            try:
                source_node, _source_local = self._graph.resolve_existing(edge.source)
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                continue
            if target_node != path:
                continue
            source = self._graph.nodes[source_node]
            result[target_local] = (source.state, source.current_checksum)
        return result

    def _apply_upstream_state(self, node: Node, state: str, checksum) -> None:
        if state == "complete":
            node.current_checksum = checksum
            node.state = "complete"
            node.block_reason = None
        elif state == "failed":
            node.current_checksum = None
            node.state = "blocked"
            node.block_reason = "blocked-by-error"
        elif state == "unwired":
            node.current_checksum = None
            node.state = "blocked"
            node.block_reason = "blocked-by-unwired"
        else:
            node.current_checksum = None
            node.state = "waiting"
            node.block_reason = None

    def _get_checksum(self, node_path: NodePath, local: tuple[str, ...]):
        node = self._graph.nodes[node_path]
        if local == ():
            return node.current_checksum
        if node.kind == "cell":
            producer = node.cell_overlay.entries.get(local)
            if producer:
                return producer.checksum
            value = node.current_checksum.resolve(node.cell_config.celltype) if node.current_checksum else None
            if isinstance(value, dict):
                return checksum_for_value(value.get(local[0]), node.cell_config.celltype)
        return None

    def _get_buffer(self, node_path, local):
        checksum = self._get_checksum(node_path, local)
        return buffer_for_checksum(checksum)

    def _get_value(self, node_path, local, *, celltype: str | None = None):
        node = self._graph.nodes[node_path]
        if celltype is None:
            celltype = node.cell_config.celltype if node.kind == "cell" else "mixed"
        checksum = self._get_checksum(node_path, local)
        return value_for_checksum(checksum, celltype) if checksum is not None else None

    def _get_transformer_pin_checksum(self, node_path, local):
        overlay = self._graph.nodes[node_path].transformer_pin_overlays.get(local[0])
        if overlay is None:
            return None
        producer = overlay.entries.get(tuple(local[1:]))
        return producer.checksum if producer else None

    def _get_transformer_pin_value(self, node_path, local):
        checksum = self._get_transformer_pin_checksum(node_path, local)
        if checksum is None:
            return None
        cfg = self._graph.nodes[node_path].transformer_config
        return value_for_checksum(checksum, cfg.celltypes.get(local[0], "mixed"))

    def _compute_node(self, node_path: NodePath):
        node = self._graph.nodes[node_path]
        if not self.eager:
            upstream = self._upstream_cone(node_path)
            node.active_count += 1
            for upstream_path in upstream:
                self._graph.nodes[upstream_path].derived_active_count += 1
            try:
                self._derive_all()
                if node.state == "unwired":
                    raise NodeError("Node is unwired")
                if node.state == "blocked":
                    raise NodeError(f"Node is blocked: {node.block_reason}")
                if node.state == "failed":
                    raise node.exception
                return self._get_value(node_path, ())
            finally:
                node.active_count -= 1
                for upstream_path in upstream:
                    self._graph.nodes[upstream_path].derived_active_count -= 1
                self._derive_all()
        if node.state == "unwired":
            raise NodeError("Node is unwired")
        if node.state == "blocked":
            raise NodeError(f"Node is blocked: {node.block_reason}")
        if node.state == "failed":
            raise node.exception
        return self._get_value(node_path, ())

    def _upstream_cone(self, node_path: NodePath) -> set[NodePath]:
        result: set[NodePath] = set()
        stack = [node_path]
        while stack:
            current = stack.pop()
            for edge in self._graph.edges:
                try:
                    source, _ = self._graph.resolve_existing(edge.source)
                    target, _ = self._graph.resolve_existing(edge.target)
                except KeyError:
                    continue
                if target == current and source not in result:
                    result.add(source)
                    stack.append(source)
        result.discard(node_path)
        return result

    def _build_cell_expression(self, node_path, local, input_ref):
        from seamless import Expression
        from seamless.cell_class import _UNSET

        if input_ref is _UNSET:
            input_ref = self._get_checksum(node_path, local)
        node = self._graph.nodes[node_path]
        return Expression(input_ref, celltype=node.cell_config.celltype, target_celltype=node.cell_config.target_celltype)

    def _clear_exception(self, node_path: NodePath):
        node = self._graph.nodes[node_path]
        node.exception = None
        record = self._runtime.current_runs.get(node_path)
        if record is not None:
            record.exception = None
        self._derive_all()
        return None

    def prune(self, node_path: NodePath | None = None):
        paths = None
        if node_path is not None:
            paths = {node_path} | self._downstream_cone(node_path)
        return {"cancelled": self._runtime.prune(paths)}

    def _downstream_cone(self, node_path: NodePath) -> set[NodePath]:
        result: set[NodePath] = set()
        stack = [node_path]
        while stack:
            current = stack.pop()
            for edge in self._graph.edges:
                try:
                    source, _ = self._graph.resolve_existing(edge.source)
                    target, _ = self._graph.resolve_existing(edge.target)
                except KeyError:
                    continue
                if source == current and target not in result:
                    result.add(target)
                    stack.append(target)
        return result

    def translate(self):
        return None

    def get_graph(self, runtime: bool = False) -> dict[str, Any]:
        nodes = []
        for path, node in sorted(self._graph.nodes.items()):
            if node.kind == "cell":
                entry = {
                    "type": "cell",
                    "path": list(path),
                    "celltype": node.cell_config.celltype,
                    "target_celltype": node.cell_config.target_celltype,
                    "overlay": [
                        {"path": list(local), "checksum": producer.checksum.hex(), "celltype": producer.celltype}
                        for local, producer in sorted(node.cell_overlay.entries.items())
                    ],
                }
            else:
                cfg = node.transformer_config
                entry = {
                    "type": "transformer",
                    "path": list(path),
                    "language": cfg.language,
                    "pins": {pin: {"celltype": cfg.celltypes.get(pin, "mixed")} for pin in sorted(cfg.pins)},
                    "optional_pins": sorted(cfg.optional_pins),
                    "checksum": {"code": cfg.code_checksum.hex() if cfg.code_checksum else None},
                    "overlays": {
                        pin: [
                            {"path": list(local), "checksum": producer.checksum.hex(), "celltype": producer.celltype}
                            for local, producer in sorted(overlay.entries.items())
                        ]
                        for pin, overlay in sorted(node.transformer_pin_overlays.items())
                    },
                }
            if runtime:
                entry["runtime"] = {
                    "state": node.state,
                    "block_reason": node.block_reason,
                    "checksum": node.current_checksum.hex() if node.current_checksum else None,
                    "exception": type(node.exception).__name__ if node.exception else None,
                    "run": self._runtime_graph_entry(path),
                }
            nodes.append(entry)
        return {
            "__seamless_workflow__": "0.1",
            "nodes": nodes,
            "connections": [
                {"type": "connection", "source": list(edge.source), "target": list(edge.target)}
                for edge in sorted(self._graph.edges, key=lambda e: (e.source, e.target))
            ],
            "params": {"eager": self.eager},
        }

    def set_graph(self, graph: dict[str, Any]) -> None:
        self._graph = ContextGraph()
        self._runtime = ContextRuntime()
        self.eager = graph.get("params", {}).get("eager", True)
        for entry in graph.get("nodes", []):
            path = tuple(entry["path"])
            if entry["type"] == "cell":
                node = self._create_cell(path, celltype=entry.get("celltype", "mixed"))
                node.cell_config.target_celltype = entry.get("target_celltype", node.cell_config.celltype)
                for producer in entry.get("overlay", []):
                    node.cell_overlay.entries[tuple(producer["path"])] = ConstantProducer(
                        Checksum(producer["checksum"]), producer.get("celltype", node.cell_config.celltype)
                    )
            elif entry["type"] == "transformer":
                cfg = TransformerConfig(
                    code_checksum=Checksum(entry["checksum"]["code"]) if entry.get("checksum", {}).get("code") else None,
                    language=entry.get("language", "python"),
                    pins=set(entry.get("pins", {})),
                    celltypes={pin: meta.get("celltype", "mixed") for pin, meta in entry.get("pins", {}).items()},
                    optional_pins=set(entry.get("optional_pins", [])),
                )
                cfg.celltypes.setdefault("result", "mixed")
                overlays = {}
                for pin, producers in entry.get("overlays", {}).items():
                    overlays[pin] = Overlay(
                        {
                            tuple(producer["path"]): ConstantProducer(
                                Checksum(producer["checksum"]),
                                producer.get("celltype", cfg.celltypes.get(pin, "mixed")),
                            )
                            for producer in producers
                        }
                    )
                self._graph.nodes[path] = Node(kind="transformer", transformer_config=cfg, transformer_pin_overlays=overlays)
        for edge in graph.get("connections", []):
            self._graph.edges.append(Edge(tuple(edge["source"]), tuple(edge["target"])))
        self._derive_all()

    def _runtime_graph_entry(self, path: NodePath) -> dict[str, Any]:
        current = self._runtime.current_runs.get(path)
        superseded = self._runtime.superseded_runs.get(path, ())
        return {
            "current": None
            if current is None
            else {
                "identity": current.identity_checksum.hex()
                if current.identity_checksum
                else None,
                "result": current.result_checksum.hex() if current.result_checksum else None,
                "phase": current.phase,
                "generation": current.generation,
                "exception": None
                if current.exception is None
                else {"type": current.exception.type, "message": current.exception.message},
            },
            "superseded": [
                {
                    "identity": record.identity_checksum.hex()
                    if record.identity_checksum
                    else None,
                    "result": record.result_checksum.hex()
                    if record.result_checksum
                    else None,
                    "phase": record.phase,
                    "generation": record.generation,
                    "hold_kind": record.hold_kind,
                    "hold_deadline": record.hold_deadline,
                }
                for record in superseded
            ],
        }

    def _copy_subcontext(self, source_prefix: NodePath, target_prefix: NodePath) -> None:
        self._graph.namespaces.add(target_prefix)
        for path in self._graph.descendants(source_prefix):
            suffix = path[len(source_prefix) :]
            self._graph.nodes[target_prefix + suffix] = copy.deepcopy(self._graph.nodes[path])
        for edge in list(self._graph.edges):
            if edge.source[: len(source_prefix)] == source_prefix and edge.target[: len(source_prefix)] == source_prefix:
                self._graph.edges.append(
                    Edge(
                        target_prefix + edge.source[len(source_prefix) :],
                        target_prefix + edge.target[len(source_prefix) :],
                        edge.source_celltype,
                        edge.target_celltype,
                    )
                )


__all__ = ["Context"]
