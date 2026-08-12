"""Reactive Context with canonical Cell and Transformer handles."""

from __future__ import annotations

import copy
import inspect
import operator
import textwrap
from typing import Any
from uuid import uuid4

from seamless import Buffer, Cell, Checksum, Expression
from seamless_transformer.builder_snapshot import TransformerBuilderSnapshot

from .adapters import buffer_for_checksum, checksum_for_value, normalize_checksum, value_for_checksum
from .builder_state import BoundCellBackend, BoundTransformerBackend, _path_string
from .endpoints import BoundEndpoint
from .errors import (
    AuthorityError,
    DependencyError,
    NodeError,
    PathError,
    ReadOnlyEndpointError,
    StaleWorkflowHandleError,
    ValueUnavailableError,
)
from .graph import CellConfig, ConstantProducer, ContextGraph, Edge, Node, NodePath, TransformerConfig
from .scheduler import ContextRuntime, ExceptionInfo, RunRecord
from .views import MissingView, SubContextView

PIN_CELLTYPES = {"plain", "mixed", "deepcell", "deepfolder", "folder"}


class Context:
    def __init__(self, eager: bool = True) -> None:
        object.__setattr__(self, "top_id", uuid4().hex)
        object.__setattr__(self, "_graph", ContextGraph())
        object.__setattr__(self, "_runtime", ContextRuntime())
        object.__setattr__(self, "eager", bool(eager))
        object.__setattr__(self, "_prefix", ())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._lookup(self._prefix + (name,))

    def __setattr__(self, name, value):
        if name.startswith("_") or name in {"top_id", "eager"}:
            object.__setattr__(self, name, value)
        else:
            self._assign(self._prefix + (name,), value)

    def __delattr__(self, name):
        if name.startswith("_"):
            object.__delattr__(self, name)
        else:
            self._delete(self._prefix + (name,))

    def __getitem__(self, key):
        return self._lookup(self._prefix + (str(key),))

    def __setitem__(self, key, value):
        self._assign(self._prefix + (str(key),), value)

    def __delitem__(self, key):
        self._delete(self._prefix + (str(key),))

    def _lookup(self, path):
        path = tuple(path)
        if path in self._graph.nodes:
            node = self._graph.nodes[path]
            if node.kind == "cell":
                return Cell._from_backend(BoundCellBackend(self, path))
            return self._transformer_handle(path)
        if path in self._graph.namespaces or self._graph.has_prefix(path):
            return SubContextView(self, path)
        return MissingView(self, path)

    def _transformer_handle(self, path):
        from seamless_transformer.transformer_class import DirectTransformer, Transformer

        node = self._graph.nodes[path]
        cls = DirectTransformer if node.transformer_config.call_mode == "direct" else Transformer
        handle = cls.__new__(cls)
        object.__setattr__(handle, "_workflow_backend", BoundTransformerBackend(self, path))
        return handle

    def _assign(self, path: NodePath, value: Any) -> None:
        from seamless_transformer.transformer_class import Transformer

        path = tuple(path)
        if path in self._graph.nodes:
            node = self._graph.nodes[path]
            if node.kind == "cell":
                if self._is_bound_source(value):
                    self._replace_edges_to(path)
                    self._add_endpoint_edge(value, self._cell_endpoint(path))
                elif isinstance(value, Cell):
                    self._replace_cell_from_builder(path, value)
                elif callable(value):
                    raise NodeError("Cannot replace a cell node with transformer code")
                else:
                    self._cell_operation(path, (), value)
            else:
                if self._is_bound_source(value):
                    self._replace_edges_to(path)
                    self._create_cell(path)
                    self._add_endpoint_edge(value, self._cell_endpoint(path))
                elif isinstance(value, Transformer):
                    self._replace_transformer_from_builder(path, value)
                elif callable(value) or isinstance(value, str):
                    self._set_transformer_code(path, value)
                else:
                    raise NodeError("Cannot replace a transformer node with a non-transformer value")
            self._derive_all()
            return

        if isinstance(value, Context):
            self._graph.namespaces.add(path)
        elif isinstance(value, SubContextView):
            self._copy_subcontext(value._prefix, path)
        elif self._is_bound_source(value):
            self._create_cell(path)
            self._add_endpoint_edge(value, self._cell_endpoint(path))
        elif isinstance(value, Cell):
            self._create_cell_from_builder(path, value)
        elif isinstance(value, Transformer):
            self._create_transformer_from_builder(path, value)
        elif callable(value):
            self._create_transformer(path, value)
        else:
            self._create_cell(path)
            self._cell_operation(path, (), value)
        self._derive_all()

    def _delete(self, path):
        path = tuple(path)
        if path in self._graph.nodes:
            descendants = self._graph.descendants(path)
            for node_path in descendants:
                self._graph.nodes.pop(node_path, None)
                self._runtime.current_runs.pop(node_path, None)
                self._runtime.superseded_runs.pop(node_path, None)
            self._graph.edges = [
                edge for edge in self._graph.edges
                if not edge.source[: len(path)] == path and not edge.target[: len(path)] == path
            ]
            self._derive_all()
            return
        if path in self._graph.namespaces:
            self._graph.namespaces = {p for p in self._graph.namespaces if p[: len(path)] != path}
            self._derive_all()
            return
        try:
            node_path, local = self._graph.resolve_existing(path)
        except KeyError:
            return
        if self._graph.nodes[node_path].kind == "cell":
            self._cell_delete_path(node_path, local)
        elif len(local) == 1:
            self._delete_transformer_pin(node_path, local[0])

    def _create_cell(self, path, *, celltype="mixed"):
        if path in self._graph.nodes:
            raise NodeError(path)
        node = Node(kind="cell", cell_config=CellConfig(celltype=celltype, target_celltype=celltype))
        self._graph.nodes[path] = node
        return node

    def _create_cell_from_builder(self, path, cell):
        self._create_cell(path, celltype=cell.celltype)
        self._replace_cell_from_builder(path, cell)

    def _replace_cell_from_builder(self, path, cell):
        node = self._graph.nodes[path]
        node.cell_config = CellConfig(cell.celltype, cell.target_celltype, cell.validator, cell.validator_language)
        input_ref = cell.input_ref
        if isinstance(input_ref, Cell):
            if input_ref._workflow_backend is not None:
                self._add_endpoint_edge(input_ref, self._cell_endpoint(path))
            else:
                upstream = self._graph.first_free("cell")
                self._create_cell_from_builder(upstream, input_ref)
                self._add_edge(upstream, path)
        elif self._is_bound_source(input_ref):
            self._add_endpoint_edge(input_ref, self._cell_endpoint(path))
        elif input_ref is not None:
            self._set_cell_root(path, checksum_for_value(input_ref, cell.celltype), cell.celltype)
        object.__setattr__(cell, "_workflow_backend", BoundCellBackend(self, path))

    def _transformer_config_from_snapshot(self, snapshot, *, direct=False):
        cfg = TransformerConfig(
            code=snapshot.codebuf,
            code_checksum=snapshot.codebuf.get_checksum(),
            language=snapshot.language,
            callable=snapshot.callable,
            pins=set(snapshot.celltypes) - {"result"},
            celltypes=copy.deepcopy(snapshot.celltypes),
            optional_pins=set(snapshot.optional_pins),
            modules=copy.deepcopy(snapshot.modules),
            globals=copy.deepcopy(snapshot.globals),
            meta=copy.deepcopy(snapshot.meta),
            environment=copy.deepcopy(snapshot.environment),
            scratch=snapshot.scratch,
            local=snapshot.local,
            direct_print=snapshot.direct_print,
            call_mode="direct" if direct else snapshot.call_mode,
        )
        cfg.pins.update(snapshot.args)
        return cfg, snapshot

    def _transformer_config_from_code(self, code):
        if callable(code):
            try:
                source = inspect.getsource(code)
            except (OSError, TypeError):
                # The callable remains the executable source for the reactive
                # node; an empty code buffer is only a static fallback for
                # interactive functions whose source cannot be inspected.
                source = ""
            buf = Buffer(source, "python")
            signature = inspect.signature(code)
            return TransformerConfig(
                code=buf,
                code_checksum=buf.get_checksum(),
                language="python",
                callable=code,
                pins=set(signature.parameters),
                celltypes={**{p: "mixed" for p in signature.parameters}, "result": "mixed"},
                meta={"local": False},
            )
        buf = Buffer(str(code), "text")
        return TransformerConfig(code=buf, code_checksum=buf.get_checksum(), language="text", meta={"local": False})

    def _create_transformer(self, path, code):
        self._graph.nodes[path] = Node(kind="transformer", transformer_config=self._transformer_config_from_code(code))

    def _create_transformer_from_builder(self, path, transformer):
        snapshot = transformer._snapshot_for_call()
        cfg, snapshot = self._transformer_config_from_snapshot(
            snapshot, direct=transformer.__class__.__name__ == "DirectTransformer"
        )
        self._graph.nodes[path] = Node(kind="transformer", transformer_config=cfg)
        for pin, value in snapshot.args.items():
            self._set_transformer_pin(path, pin, value)
        object.__setattr__(transformer, "_workflow_backend", BoundTransformerBackend(self, path))

    def _replace_transformer_from_builder(self, path, transformer):
        node = self._graph.nodes[path]
        snapshot = transformer._snapshot_for_call()
        cfg, snapshot = self._transformer_config_from_snapshot(snapshot, direct=transformer.__class__.__name__ == "DirectTransformer")
        cfg.pins.update(node.transformer_config.pins)
        node.transformer_config = cfg
        node.transformer_pin_producers.clear()
        for pin, value in snapshot.args.items():
            self._set_transformer_pin(path, pin, value)
        object.__setattr__(transformer, "_workflow_backend", BoundTransformerBackend(self, path))

    def _set_cell_config(self, path, **updates):
        cfg = self._graph.nodes[path].cell_config
        for key, value in updates.items():
            if key == "target_celltype" and value is None:
                value = cfg.celltype
            setattr(cfg, key, value)
        self._derive_all()

    def _set_cell_root(self, path, checksum, celltype):
        self._set_cell_root_with_edges(path, checksum, celltype, clear_edges=True)

    def _set_cell_root_with_edges(self, path, checksum, celltype, *, clear_edges):
        node = self._graph.nodes[path]
        node.cell_root_producer = ConstantProducer(normalize_checksum(checksum), celltype)
        if clear_edges:
            self._remove_edges_targeting(path, (), descendants=True)

    def _set_cell_value(self, path, local, value):
        self._cell_operation(path, local, value)

    def _set_cell_checksum(self, path, local, checksum, *, celltype=None):
        self._cell_operation(path, local, Checksum(checksum), checksum_rhs=True)

    def _cell_operation(self, node_path, local, value, *, checksum_rhs=False):
        node = self._graph.nodes[node_path]
        if node.kind != "cell":
            raise NodeError(node_path)
        local = tuple(local)
        endpoint = self._endpoint(value)
        if endpoint is not None:
            if len(local) > 1:
                raise PathError("Cell source connections are limited to root and one level")
            self._add_endpoint_edge(value, self._cell_endpoint(node_path, local))
            self._derive_all()
            return
        if checksum_rhs:
            try:
                value = value_for_checksum(value, node.cell_config.celltype)
            except Exception as exc:
                raise ValueUnavailableError(f"Cannot materialize {node_path!r}{local!r}") from exc
        if not local:
            if self._incoming_edge(node_path, ()) is not None:
                raise AuthorityError(f"Cell {node_path!r} root is controlled by an incoming edge")
            self._set_cell_root(node_path, checksum_for_value(value, node.cell_config.celltype), node.cell_config.celltype)
            self._derive_all()
            return
        self._check_cell_update_authority(node_path, local)
        root = self._materialize_cell_for_update(node_path, local)
        updated = copy.deepcopy(root)
        _assign_path(updated, local, value)
        self._set_cell_root_with_edges(node_path, checksum_for_value(updated, node.cell_config.celltype), node.cell_config.celltype, clear_edges=False)
        self._remove_edges_targeting(node_path, local, descendants=True)
        self._derive_all()

    def _materialize_cell_for_update(self, node_path, local):
        node = self._graph.nodes[node_path]
        if node.state == "unwired" and node.cell_root_producer is None and not self._incoming_for(node_path):
            if isinstance(local[0], str):
                return {}
            raise TypeError(f"Cannot create sequence path {local!r} on an unwired Cell")
        if node.state != "complete" or node.current_checksum is None:
            raise ValueUnavailableError(f"Value for Cell {node_path!r} is unavailable")
        try:
            value = value_for_checksum(node.current_checksum, node.cell_config.celltype)
        except Exception as exc:
            raise ValueUnavailableError(f"Value for Cell {node_path!r} is unavailable") from exc
        return value

    def _check_cell_update_authority(self, node_path, local):
        incoming = self._incoming_for(node_path)
        if () in incoming or (len(local) > 1 and (local[0],) in incoming):
            raise AuthorityError(f"Cell {node_path!r}{local!r} is controlled by an incoming edge")

    def _cell_delete_path(self, node_path, local):
        local = tuple(local)
        if not local:
            self._graph.nodes[node_path].cell_root_producer = None
            self._remove_edges_targeting(node_path, (), descendants=True)
            self._derive_all()
            return
        edge = self._incoming_edge(node_path, local)
        if len(local) == 1 and edge is not None:
            self._graph.edges.remove(edge)
            self._derive_all()
            return
        self._check_cell_update_authority(node_path, local)
        root = self._materialize_cell_for_update(node_path, local)
        updated = copy.deepcopy(root)
        _delete_path(updated, local)
        self._set_cell_root_with_edges(node_path, checksum_for_value(updated, self._graph.nodes[node_path].cell_config.celltype), self._graph.nodes[node_path].cell_config.celltype, clear_edges=False)
        self._remove_edges_targeting(node_path, local, descendants=True)
        self._derive_all()

    def _set_transformer_code(self, node_path, code):
        endpoint = self._endpoint(code)
        if endpoint is not None:
            self._add_endpoint_edge(code, self._transformer_endpoint(node_path, "code"))
            self._graph.nodes[node_path].transformer_config.code_checksum = None
            self._derive_all()
            return
        cfg = self._transformer_config_from_code(code)
        node = self._graph.nodes[node_path]
        old = node.transformer_config
        cfg.pins.update(old.pins)
        cfg.optional_pins = old.optional_pins & cfg.pins
        node.transformer_config = cfg
        self._derive_all()

    def _set_transformer_pin(self, node_path, pin, value):
        if not isinstance(pin, str) or not pin:
            raise PathError("Transformer pin name must be non-empty")
        cfg = self._graph.nodes[node_path].transformer_config
        cfg.check_pin_name(pin)
        cfg.pins.add(pin)
        cfg.celltypes.setdefault(pin, "mixed")
        endpoint = self._endpoint(value)
        if endpoint is not None:
            self._add_endpoint_edge(value, self._transformer_endpoint(node_path, pin))
        else:
            checksum = checksum_for_value(
                value, cfg.celltypes.get(pin, "mixed"), retain=True
            )
            self._check_authority(node_path, (pin,))
            self._graph.nodes[node_path].transformer_pin_producers[pin] = ConstantProducer(checksum, cfg.celltypes.get(pin, "mixed"))
            self._remove_edges_targeting(node_path, (pin,), descendants=True)
        self._derive_all()

    def _delete_transformer_pin(self, node_path, pin):
        if not pin:
            raise PathError("Transformer pin name must be non-empty")
        self._graph.nodes[node_path].transformer_pin_producers.pop(pin, None)
        self._remove_edges_targeting(node_path, (pin,), descendants=True)
        self._derive_all()

    def _endpoint(self, value):
        method = getattr(value, "_workflow_endpoint", None)
        if not callable(method):
            return None
        endpoint = method()
        return endpoint if isinstance(endpoint, BoundEndpoint) else None

    def _is_bound_source(self, value):
        return self._endpoint(value) is not None

    def _source_path(self, value):
        endpoint = self._endpoint(value)
        if endpoint is None:
            raise DependencyError(f"Not a workflow source: {value!r}")
        if endpoint.top_id != self.top_id:
            raise DependencyError("Cross-top-level dependencies are not supported")
        if not endpoint.can_source:
            raise DependencyError("Endpoint cannot be used as a source")
        return endpoint.node_path + endpoint.local_path

    def _cell_endpoint(self, node_path, local=()):
        return BoundEndpoint(self.top_id, tuple(node_path), "cell-result" if not local else "cell-subvalue", tuple(local), True, True, True)

    def _transformer_endpoint(self, node_path, pin=None):
        return BoundEndpoint(self.top_id, tuple(node_path), "transformer-code" if pin == "code" else "transformer-input", () if pin is None else (pin,), True, True, True)

    def _add_endpoint_edge(self, source, target):
        source_ep = self._endpoint(source)
        if source_ep is None:
            raise DependencyError("Source is not a bound workflow endpoint")
        if source_ep.top_id != self.top_id or target.top_id != self.top_id:
            raise DependencyError("Cross-top-level dependencies are not supported")
        if not source_ep.can_source:
            raise DependencyError("Endpoint cannot be used as a source")
        if target.endpoint_kind == "cell-subvalue":
            if len(target.local_path) != 1 or isinstance(target.local_path[0], slice):
                raise PathError("Cell connection targets are limited to one point component")
            if self._graph.nodes[target.node_path].cell_config.celltype not in PIN_CELLTYPES:
                raise PathError("Cell subvalue connections require a container-capable Cell")
            component = target.local_path[0]
            if isinstance(component, int):
                current = self._get_value(target.node_path, ())
                if not isinstance(current, (list, tuple)):
                    raise TypeError("Integer Cell connection targets require an existing sequence")
                if component >= len(current) or component < -len(current):
                    raise IndexError(component)
        elif target.endpoint_kind == "transformer-input":
            if len(target.local_path) != 1:
                raise PathError("Transformer targets must address a whole pin")
        self._add_edge(source_ep.node_path + source_ep.local_path, target.node_path + target.local_path)

    def _add_edge(self, source, target):
        source_node, _ = self._graph.resolve_existing(tuple(source))
        target_node, target_local = self._graph.resolve_existing(tuple(target))
        if source_node == target_node:
            raise DependencyError("Self-dependencies are not supported")
        if self._would_cycle(source_node, target_node):
            raise DependencyError("Dependency cycle")
        self._check_authority(target_node, target_local)
        self._remove_edges_targeting(target_node, target_local, descendants=True)
        self._graph.edges.append(Edge(tuple(source), tuple(target)))
        self._derive_all()

    def _would_cycle(self, source, target):
        seen = set()
        stack = [source]
        while stack:
            current = stack.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            for edge in self._graph.edges:
                try:
                    s, _ = self._graph.resolve_existing(edge.source)
                    t, _ = self._graph.resolve_existing(edge.target)
                except KeyError:
                    continue
                if s == current:
                    stack.append(t)
        return False

    def _check_authority(self, node_path, local):
        if self._incoming_edge(node_path, local) is not None:
            raise AuthorityError(f"Endpoint {node_path!r}{local!r} has an incoming producer")

    def _replace_edges_to(self, node_path):
        self._graph.edges = [e for e in self._graph.edges if self._target_node(e) != node_path]

    def _target_node(self, edge):
        try:
            return self._graph.resolve_existing(edge.target)[0]
        except KeyError:
            return None

    def _remove_edges_targeting(self, node_path, local, *, descendants=False):
        kept = []
        for edge in self._graph.edges:
            try:
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                kept.append(edge)
                continue
            match = target_node == node_path and (target_local[: len(local)] == local if descendants else target_local == local)
            if not match:
                kept.append(edge)
        self._graph.edges = kept

    def _incoming_edge(self, node_path, local):
        for edge in self._graph.edges:
            try:
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                continue
            if target_node == node_path and target_local == tuple(local):
                return edge
        return None

    def _incoming_for(self, node_path):
        result = {}
        for edge in self._graph.edges:
            try:
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                continue
            if target_node == node_path:
                result[target_local] = edge
        return result

    def _value_from_producer(self, producer):
        return value_for_checksum(producer.checksum, producer.celltype)

    def _derive_all(self):
        # A source may sort after its target; converge the small durable graph
        # rather than making public state depend on lexical node names.
        for _ in range(max(1, len(self._graph.nodes))):
            before = tuple(
                (path, self._graph.nodes[path].state, self._graph.nodes[path].current_checksum.hex() if self._graph.nodes[path].current_checksum else None)
                for path in sorted(self._graph.nodes)
            )
            for path in sorted(self._graph.nodes):
                self._derive_node(path)
            after = tuple(
                (path, self._graph.nodes[path].state, self._graph.nodes[path].current_checksum.hex() if self._graph.nodes[path].current_checksum else None)
                for path in sorted(self._graph.nodes)
            )
            if before == after:
                break
        for path in sorted(self._graph.nodes):
            self._update_runtime(path)

    def _derive_node(self, path):
        node = self._graph.nodes[path]
        if node.kind == "cell":
            self._derive_cell(path, node)
        else:
            self._derive_transformer(path, node)

    def _derive_cell(self, path, node):
        incoming = self._incoming_for(path)
        if () in incoming:
            self._apply_upstream_state(node, self._source_state(incoming[()]))
            return
        if node.cell_root_producer is not None and not any(len(local) for local in incoming):
            node.current_checksum = node.cell_root_producer.checksum
            node.state, node.block_reason, node.exception = "complete", None, None
            return
        base = None
        if node.cell_root_producer is not None:
            base = self._value_from_producer(node.cell_root_producer)
        elif incoming:
            base = {} if all(isinstance(local[0], str) for local in incoming if local) else None
        if not incoming and base is None:
            node.current_checksum, node.state, node.block_reason = None, "unwired", None
            return
        if base is None:
            self._apply_pending(node, incoming.values())
            return
        result = copy.deepcopy(base)
        for local, edge in incoming.items():
            if not local:
                continue
            state, checksum = self._source_state(edge)
            if state != "complete":
                self._apply_pending(node, [edge])
                return
            if len(local) != 1:
                node.state = "failed"; node.exception = PathError("Cell target deeper than one component"); node.current_checksum = None; return
            _assign_path(result, local, value_for_checksum(checksum, "mixed"))
        try:
            node.current_checksum = checksum_for_value(result, node.cell_config.celltype)
            node.state, node.block_reason, node.exception = "complete", None, None
        except Exception as exc:
            node.state, node.exception, node.current_checksum = "failed", exc, None

    def _derive_transformer(self, path, node):
        cfg = node.transformer_config
        incoming = self._incoming_for(path)
        code_checksum = cfg.code_checksum
        if ("code",) in incoming:
            state, code_checksum = self._source_state(incoming[("code",)])
            if state != "complete":
                self._apply_pending(node, [incoming[("code",)]]); return
        if code_checksum is None:
            node.state, node.current_checksum = "unwired", None
            return
        try:
            kwargs = {}
            for pin in sorted(cfg.pins):
                edge = incoming.get((pin,))
                if edge is not None:
                    state, checksum = self._source_state(edge)
                    if state != "complete":
                        self._apply_pending(node, [edge]); return
                else:
                    producer = node.transformer_pin_producers.get(pin)
                    checksum = producer.checksum if producer is not None else None
                if checksum is None:
                    if pin in cfg.optional_pins:
                        continue
                    node.state, node.current_checksum, node.block_reason = "unwired", None, None
                    return
                kwargs[pin] = value_for_checksum(
                    checksum, cfg.celltypes.get(pin, "mixed")
                )
            if cfg.callable is None:
                node.state, node.current_checksum, node.block_reason = "complete", None, None
                return
            if not self.eager and node.active_count == 0 and node.derived_active_count == 0:
                node.state, node.current_checksum = "waiting", None
                return
            result = cfg.callable(**kwargs)
            node.current_checksum = checksum_for_value(result, cfg.celltypes.get("result", "mixed"))
            node.state, node.block_reason, node.exception = "complete", None, None
        except BaseException as exc:
            node.state, node.current_checksum, node.block_reason, node.exception = (
                "failed",
                None,
                None,
                exc,
            )

    def _source_state(self, edge):
        source_node, source_local = self._graph.resolve_existing(edge.source)
        node = self._graph.nodes[source_node]
        if node.state != "complete":
            return node.state, None
        return "complete", self._get_checksum(source_node, source_local)

    def _apply_pending(self, node, edges):
        states = [self._source_state(edge)[0] for edge in edges]
        state = "failed" if "failed" in states or "blocked" in states else ("waiting" if "waiting" in states or "computing" in states else "blocked")
        node.state = "blocked" if state in {"failed", "blocked"} else state
        node.block_reason = "blocked-by-error" if state in {"failed", "blocked"} else None
        node.current_checksum = None

    def _apply_upstream_state(self, node, upstream):
        state, checksum = upstream
        if state == "complete":
            node.current_checksum = checksum
            node.state, node.block_reason, node.exception = "complete", None, None
        elif state == "failed":
            node.current_checksum, node.state, node.block_reason = None, "blocked", "blocked-by-error"
        elif state in {"blocked", "unwired"}:
            node.current_checksum, node.state, node.block_reason = None, "blocked", "blocked-by-unwired"
        else:
            node.current_checksum, node.state, node.block_reason = None, "waiting", None

    def _get_checksum(self, node_path, local=()):
        node = self._graph.nodes[node_path]
        local = tuple(local)
        if node.kind == "transformer":
            if local:
                edge = self._incoming_edge(node_path, local)
                if edge:
                    return self._get_checksum(*self._graph.resolve_existing(edge.source))
                producer = node.transformer_pin_producers.get(local[0])
                return producer.checksum if producer and len(local) == 1 else None
            return node.current_checksum
        if not local:
            return node.current_checksum
        if node.current_checksum is None:
            return None
        try:
            value = value_for_checksum(node.current_checksum, node.cell_config.celltype)
            value = _read_path(value, local)
            return checksum_for_value(value, node.cell_config.celltype)
        except (KeyError, IndexError, TypeError):
            return None

    def _get_value(self, node_path, local=(), *, celltype=None):
        checksum = self._get_checksum(node_path, local)
        if checksum is None:
            return None
        node = self._graph.nodes[node_path]
        if celltype is None:
            celltype = node.cell_config.celltype if node.kind == "cell" else node.transformer_config.celltypes.get("result", "mixed")
        return value_for_checksum(checksum, celltype)

    def _get_buffer(self, node_path, local=()):
        checksum = self._get_checksum(node_path, local)
        return buffer_for_checksum(checksum) if checksum is not None else None

    def _compute_node(self, node_path, *, reactive=True, checksum=False):
        node = self._graph.nodes[node_path]
        upstream = self._upstream_cone(node_path)
        if not self.eager:
            node.active_count += 1
            for p in upstream: self._graph.nodes[p].derived_active_count += 1
        try:
            self._derive_all()
            node = self._graph.nodes[node_path]
            if node.state == "unwired": raise NodeError("Node is unwired")
            if node.state == "blocked": raise NodeError(f"Node is blocked: {node.block_reason}")
            if node.state == "failed": raise node.exception
            return node.current_checksum if checksum else self._get_value(node_path, ())
        finally:
            if not self.eager:
                node.active_count -= 1
                for p in upstream: self._graph.nodes[p].derived_active_count -= 1
                self._derive_all()

    def _compute_cell_endpoint(self, node_path, local):
        self._compute_node(node_path)
        return self._get_checksum(node_path, local)

    def _compute_cell_value(self, node_path, local):
        self._compute_node(node_path)
        return self._get_value(node_path, local)

    def _upstream_cone(self, node_path):
        result, stack = set(), [node_path]
        while stack:
            current = stack.pop()
            for edge in self._graph.edges:
                try: source, _ = self._graph.resolve_existing(edge.source); target, _ = self._graph.resolve_existing(edge.target)
                except KeyError: continue
                if target == current and source not in result:
                    result.add(source); stack.append(source)
        result.discard(node_path)
        return result

    def _build_cell_expression(self, node_path, local, input_ref):
        from seamless.cell_class import _UNSET
        if input_ref is _UNSET:
            input_ref = self._get_checksum(node_path, ())
        if input_ref is None:
            raise ValueError(f"Cannot build unwired Cell {node_path!r}")
        node = self._graph.nodes[node_path]
        return Expression(input_ref, path=_path_string(tuple(local)), celltype=node.cell_config.celltype, target_celltype=node.cell_config.target_celltype)

    def _build_source_expression(self, source):
        node_path, local = self._graph.resolve_existing(source)
        checksum = self._get_checksum(node_path, ())
        if checksum is None:
            raise ValueUnavailableError(f"Source {source!r} is not current")
        node = self._graph.nodes[node_path]
        celltype = node.cell_config.celltype if node.kind == "cell" else node.transformer_config.celltypes.get("result", "mixed")
        return Expression(checksum, path=_path_string(local), celltype=celltype, target_celltype=celltype)

    def _capture_endpoint(self, endpoint):
        node = self._graph.nodes.get(endpoint.node_path)
        if node is None:
            raise StaleWorkflowHandleError(f"Endpoint {endpoint.node_path!r} is stale")
        if node.state == "waiting": raise NotImplementedError("Capturing waiting workflow sources requires future-wired E/T")
        if node.state in {"unwired", "blocked"}: raise ValueError(f"Cannot capture workflow source in state {node.state!r}")
        checksum = self._get_checksum(endpoint.node_path, endpoint.local_path)
        if checksum is None:
            raise ValueError("Workflow source has no concrete checksum")
        return checksum

    def _public_source(self, source):
        node_path, local = self._graph.resolve_existing(source)
        node = self._graph.nodes[node_path]
        if node.kind == "cell":
            return Cell._from_backend(BoundCellBackend(self, node_path, local))
        return Cell._from_backend(BoundCellBackend(self, node_path, local, readonly=True))

    def _clear_exception(self, node_path):
        self._graph.nodes[node_path].exception = None
        self._derive_all()

    def prune(self, node_path=None):
        paths = None if node_path is None else {node_path} | self._downstream_cone(node_path)
        return {"cancelled": self._runtime.prune(paths)}

    def _downstream_cone(self, node_path):
        result, stack = set(), [node_path]
        while stack:
            current = stack.pop()
            for edge in self._graph.edges:
                try: source, _ = self._graph.resolve_existing(edge.source); target, _ = self._graph.resolve_existing(edge.target)
                except KeyError: continue
                if source == current and target not in result: result.add(target); stack.append(target)
        return result

    def _update_runtime(self, path):
        node = self._graph.nodes[path]
        identity = (path, node.current_checksum.hex() if node.current_checksum else None, node.state)
        current = self._runtime.current_runs.get(path)
        identity_checksum = checksum_for_value(identity, "plain")
        if current and current.identity_checksum == identity_checksum:
            current.result_checksum = node.current_checksum
            return
        if current is not None:
            self._runtime.supersede(path)
        if node.state in {"complete", "failed", "computing"}:
            self._runtime.current_runs[path] = RunRecord(path, None, identity_checksum, node.current_checksum, ExceptionInfo.from_exception(node.exception) if node.exception else None, "completed" if node.state in {"complete", "failed"} else "running", self._runtime.next_generation())

    def _runtime_graph_entry(self, path):
        current = self._runtime.current_runs.get(path)
        superseded = self._runtime.superseded_runs.get(path, ())
        record = lambda item: {"identity": item.identity_checksum.hex() if item.identity_checksum else None, "result": item.result_checksum.hex() if item.result_checksum else None, "phase": item.phase, "generation": item.generation, "hold_kind": item.hold_kind, "hold_deadline": item.hold_deadline}
        return {"current": None if current is None else {**record(current), "exception": None if current.exception is None else {"type": current.exception.type, "message": current.exception.message}}, "superseded": [record(item) for item in superseded]}

    def get_graph(self, runtime=False):
        nodes = []
        for path, node in sorted(self._graph.nodes.items()):
            if node.kind == "cell":
                entry = {"type": "cell", "path": list(path), "celltype": node.cell_config.celltype, "target_celltype": node.cell_config.target_celltype, "value": None if node.cell_root_producer is None else {"checksum": node.cell_root_producer.checksum.hex(), "celltype": node.cell_root_producer.celltype}}
            else:
                cfg = node.transformer_config
                entry = {"type": "transformer", "path": list(path), "language": cfg.language, "call_mode": cfg.call_mode, "pins": {p: {"celltype": cfg.celltypes.get(p, "mixed")} for p in sorted(cfg.pins)}, "optional_pins": sorted(cfg.optional_pins), "checksum": {"code": cfg.code_checksum.hex() if cfg.code_checksum else None}, "code": cfg.code.decode() if hasattr(cfg.code, "decode") else None, "meta": copy.deepcopy(cfg.meta), "modules": copy.deepcopy(cfg.modules), "globals": copy.deepcopy(cfg.globals), "environment": copy.deepcopy(cfg.environment), "scratch": cfg.scratch, "local": cfg.local, "direct_print": cfg.direct_print, "producers": {p: {"checksum": q.checksum.hex(), "celltype": q.celltype} for p, q in sorted(node.transformer_pin_producers.items())}}
            if runtime:
                entry["runtime"] = {"state": node.state, "block_reason": node.block_reason, "checksum": node.current_checksum.hex() if node.current_checksum else None, "exception": type(node.exception).__name__ if node.exception else None, "run": self._runtime_graph_entry(path)}
            nodes.append(entry)
        return {"__seamless_workflow__": "0.2", "nodes": nodes, "connections": [{"type": "connection", "source": list(e.source), "target": list(e.target)} for e in sorted(self._graph.edges, key=lambda e: (e.source, e.target))], "params": {"eager": self.eager}}

    def set_graph(self, graph):
        self._graph, self._runtime = ContextGraph(), ContextRuntime()
        self.eager = graph.get("params", {}).get("eager", True)
        for entry in graph.get("nodes", []):
            path = tuple(entry["path"])
            if entry["type"] == "cell":
                node = self._create_cell(path, celltype=entry.get("celltype", "mixed"))
                node.cell_config.target_celltype = entry.get("target_celltype", node.cell_config.celltype)
                if "overlay" in entry or "overlays" in entry:
                    raise PathError("Alpha overlay graph data is not supported")
                value = entry.get("value")
                if value is not None:
                    node.cell_root_producer = ConstantProducer(Checksum(value["checksum"]), value.get("celltype", node.cell_config.celltype))
            elif entry["type"] == "transformer":
                if "overlays" in entry:
                    raise PathError("Alpha transformer overlays are not supported")
                if "call_mode" not in entry:
                    raise PathError("Transformer graph entry is missing required call_mode")
                code_text = entry.get("code")
                code = Buffer(code_text, entry.get("language", "python")) if code_text is not None else None
                callable_code = None
                if code_text is not None and entry.get("language", "python") == "python":
                    namespace = {}
                    try:
                        exec(textwrap.dedent(code_text), namespace)
                    except Exception:
                        namespace = {}
                    candidates = [value for value in namespace.values() if callable(value) and not getattr(value, "__name__", "").startswith("__")]
                    for candidate in candidates:
                        try:
                            if set(inspect.signature(candidate).parameters) <= set(entry.get("pins", {})):
                                callable_code = candidate
                                break
                        except (TypeError, ValueError):
                            continue
                cfg = TransformerConfig(code=code, code_checksum=Checksum(entry["checksum"]["code"]) if entry.get("checksum", {}).get("code") else (code.get_checksum() if code is not None else None), language=entry.get("language", "python"), callable=callable_code, pins=set(entry.get("pins", {})), celltypes={p: m.get("celltype", "mixed") for p, m in entry.get("pins", {}).items()}, optional_pins=set(entry.get("optional_pins", [])), modules=copy.deepcopy(entry.get("modules", {})), globals=copy.deepcopy(entry.get("globals", {})), meta=copy.deepcopy(entry.get("meta", {})), environment=copy.deepcopy(entry.get("environment")), scratch=bool(entry.get("scratch", False)), local=entry.get("local"), direct_print=bool(entry.get("direct_print", False)), call_mode=entry["call_mode"])
                cfg.celltypes.setdefault("result", "mixed")
                producers = {p: ConstantProducer(Checksum(q["checksum"]), q.get("celltype", cfg.celltypes.get(p, "mixed"))) for p, q in entry.get("producers", {}).items()}
                self._graph.nodes[path] = Node(kind="transformer", transformer_config=cfg, transformer_pin_producers=producers)
        for edge in graph.get("connections", []):
            source, target = tuple(edge["source"]), tuple(edge["target"])
            source_node, source_local = self._graph.resolve_existing(source)
            target_node, target_local = self._graph.resolve_existing(target)
            target_kind = self._graph.nodes[target_node].kind
            if target_kind == "cell":
                if len(target_local) > 1 or any(isinstance(component, slice) for component in target_local):
                    raise PathError("Cell graph targets are limited to root or one point component")
            elif target_kind == "transformer":
                if len(target_local) != 1:
                    raise PathError("Transformer graph targets must address one whole pin")
            self._graph.edges.append(Edge(source, target))
        self._derive_all()

    def _copy_subcontext(self, source_prefix, target_prefix):
        self._graph.namespaces.add(target_prefix)
        for path in self._graph.descendants(source_prefix):
            self._graph.nodes[target_prefix + path[len(source_prefix):]] = copy.deepcopy(self._graph.nodes[path])
        for edge in list(self._graph.edges):
            if edge.source[:len(source_prefix)] == source_prefix and edge.target[:len(source_prefix)] == source_prefix:
                self._graph.edges.append(Edge(target_prefix + edge.source[len(source_prefix):], target_prefix + edge.target[len(source_prefix):], edge.source_celltype, edge.target_celltype))
        self._derive_all()


def _read_path(value, path):
    for component in path:
        value = value[component]
    return value


def _assign_path(root, path, value):
    cursor = root
    for component in path[:-1]:
        if isinstance(component, slice):
            raise TypeError("Slices cannot be intermediate update paths")
        if isinstance(cursor, dict):
            if component not in cursor:
                if not isinstance(component, str): raise TypeError("Missing mapping key for numeric path")
                cursor[component] = {}
            elif cursor[component] is None or not isinstance(cursor[component], (dict, list)):
                raise TypeError(f"Incompatible intermediate container at {component!r}")
            cursor = cursor[component]
        else:
            cursor = cursor[component]
    component = path[-1]
    if isinstance(cursor, dict): cursor[component] = value
    elif isinstance(component, slice): cursor[component] = value
    else: cursor[component] = value


def _delete_path(root, path):
    cursor = root
    for component in path[:-1]: cursor = cursor[component]
    component = path[-1]
    if isinstance(cursor, dict): cursor.pop(component, None)
    elif isinstance(component, slice): del cursor[component]
    else: del cursor[component]


__all__ = ["Context"]
