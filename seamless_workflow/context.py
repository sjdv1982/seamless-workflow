"""Reactive Context with canonical Cell and Transformer handles."""

from __future__ import annotations

import copy
import asyncio
from concurrent.futures import Future
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

from .sidework import Lease, PreparedCell, PreparedTransformer, SideLoop, evaluate_cell, evaluate_projection

PIN_CELLTYPES = {"plain", "mixed", "deepcell", "deepfolder", "folder"}



from .runtime_api import RuntimeAPI
from .reactive import Reactive
from .attachments.runtime import AttachmentRuntime


class Context(RuntimeAPI, Reactive, AttachmentRuntime):
    def __init__(self, *, expression_execution="auto") -> None:
        if expression_execution not in {"auto", "local", "remote"}:
            raise ValueError(expression_execution)
        from seamless import ensure_open
        from threading import RLock, Event
        ensure_open("Context construction")
        object.__setattr__(self, "_close_lock", RLock())
        object.__setattr__(self, "_closed_event", Event())
        object.__setattr__(self, "_closing", False)
        object.__setattr__(self, "_expression_execution", expression_execution)
        object.__setattr__(self, "top_id", uuid4().hex)
        object.__setattr__(self, "_graph", ContextGraph())
        object.__setattr__(self, "_runtime", ContextRuntime())
        object.__setattr__(self, "_refholds_released", False)
        object.__setattr__(self, "_code_refholds", {})
        object.__setattr__(self, "_module_refholds", {})
        object.__setattr__(self, "_superseded_refholds", {})
        object.__setattr__(self, "_prefix", ())
        from seamless.reference_lifecycle import register_refholder

        from .controller import Controller
        object.__setattr__(self, "_controller", Controller(self))
        object.__setattr__(self, "_side", SideLoop(f"Context-side-{self.top_id[:8]}"))
        object.__setattr__(self, "_jobs", {})
        object.__setattr__(self, "_facts", {})
        object.__setattr__(self, "_barriers", {})
        object.__setattr__(self, "_effects", [])
        object.__setattr__(self, "_mount_sessions", {})
        object.__setattr__(self, "_mount_node_leaves", {})
        object.__setattr__(self, "_mount_activity", 0)
        object.__setattr__(self, "_mount_cut_seq", 0)
        object.__setattr__(self, "_mount_close_future", None)
        from collections import deque
        object.__setattr__(self, "_mount_events", deque(maxlen=1024))
        object.__setattr__(self, "_mount_logs", set())
        object.__setattr__(self, "_revisions", {})
        register_refholder(self)
        from .lifecycle import register
        register(self)

    def __del__(self):
        try:
            controller = object.__getattribute__(self, "_controller")
            if controller.is_owner() and not controller.stopped:
                # Last-reference release may occur at the end of a controller
                # callback. Release graph roles here; joining is not a turn.
                self._closing = True
                controller.accepting = False
                self._begin_close()
                self._finish_close()
                from threading import Thread
                side = self._side
                def join():
                    side.close()
                    controller.stop()
                Thread(target=join, name="Context-finalizer", daemon=True).start()
            else:
                # Cyclic GC clears weakrefs before __del__. Retain this object
                # only for the duration of the ordered cleanup request.
                controller.context = lambda: self
                try: self.close()
                finally: controller.context = lambda: None
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _after_turn(self):
        self._controller.assert_owner()
        if self._closing: return
        self._mount_after_turn()
        self._check_barriers()
        effects, self._effects = self._effects, []
        for effect in effects:
            effect()
        self._check_barriers()
        effects, self._effects = self._effects, []
        for effect in effects: effect()

    def _node_snapshot(self, path):
        from .errors import StaleWorkflowHandleError
        if path not in self._graph.nodes:
            raise StaleWorkflowHandleError(f"Endpoint {path!r} is stale")
        return copy.deepcopy(self._graph.nodes[path])

    @property
    def mounts(self):
        from .attachments.api import ContextMounts
        return ContextMounts(self)

    def close(self, timeout=None):
        controller = getattr(self, "_controller", None)
        if controller is None: return
        if controller.is_owner():
            from .errors import ReentrantContextError
            raise ReentrantContextError("Cannot close Context from a controller turn")
        with self._close_lock:
            if self._closed_event.is_set(): return
            self._closing = True
            controller.begin_close().result()
            import time
            bound = 60 if timeout is None else timeout
            deadline = time.monotonic() + bound
            flush = controller.enqueue('_mount_close_wait', klass=1, internal=True).result()
            try: flush.result(max(0, deadline - time.monotonic()))
            except TimeoutError:
                import logging
                logging.getLogger(__name__).warning('Context mount close flush timed out')
            cleanups = controller.enqueue('_mount_close_finish', klass=1, internal=True).result()
            for cleanup in cleanups:
                if cleanup is not None:
                    try: cleanup.result(max(0, deadline - time.monotonic()))
                    except TimeoutError: pass
            self._side.close()
            controller.enqueue("_finish_close", klass=1, internal=True).result()
            controller.stop()
            self._closed_event.set()

    def _begin_close(self):
        self._closing = True
        self._effects.clear()
        from .errors import ClosedContextError
        for future in self._barriers:
            if not future.done(): future.set_exception(ClosedContextError("Context closed"))
        self._barriers.clear()
        self._mount_close_prepare()
        for task in self._jobs.values():
            if task is not None: task.cancel()
        for record in list(self._runtime.current_runs.values()) + [r for q in self._runtime.superseded_runs.values() for r in q]:
            if record.et is not None: record.et.cancel()

    def _finish_close(self):
        self._mount_close_finish()
        for leases in self._mount_node_leaves.values():
            for lease in leases: lease._release_refholds()
        self._mount_node_leaves.clear()
        self._release_refholds()
        for lease, _ in self._facts.values():
            if lease is not None: lease._release_refholds()
        self._facts.clear()
        self._jobs.clear()
        self._runtime.current_runs.clear()
        self._runtime.superseded_runs.clear()
        self._runtime.evicted.clear()
        self._graph = ContextGraph()
        self._code_refholds.clear()
        self._module_refholds.clear()
        self._superseded_refholds.clear()

    def _publish_config(self, path, revision, cfg):
        if self._revisions.get(path, 0) != revision: return False
        node = self._graph.nodes[path]
        if node.kind == "cell":
            if node.mount and (cfg.celltype, cfg.target_celltype) != (node.cell_config.celltype, node.cell_config.target_celltype):
                raise ValueError("Mounted celltype cannot change; unmount first")
            node.cell_config = cfg
        else: node.transformer_config = cfg
        self._revisions[path] = revision + 1
        self._derive_all()
        return True

    def _config_snapshot(self, path):
        node = self._graph.nodes[path]
        return copy.deepcopy(node.cell_config if node.kind == "cell" else node.transformer_config), self._revisions.get(path, 0)

    def _set_node_config(self, path, field, value, key=None, delete=False):
        raise RuntimeError("Configuration must be prepared before ingress")

    def __dir__(self):
        self._check_public_caller()
        return sorted(set(super().__dir__()) | set(self._child_names(self._prefix)))

    def _child_names(self, prefix):
        """Snapshot immediate namespace members on the controller thread."""
        size = len(prefix)
        return sorted({
            path[size]
            for path in (*self._graph.nodes, *self._graph.namespaces)
            if len(path) > size and path[:size] == prefix
        })

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._check_public_caller()
        return self._lookup(self._prefix + (name,))

    def __setattr__(self, name, value):
        if name.startswith("_") or name == "top_id":
            object.__setattr__(self, name, value)
        else:
            self._check_public_caller()
            self._assign(self._prefix + (name,), value)

    def __delattr__(self, name):
        if name.startswith("_"):
            object.__delattr__(self, name)
        else:
            self._check_public_caller()
            self._delete(self._prefix + (name,))

    def __getitem__(self, key):
        self._check_public_caller()
        return self._lookup(self._prefix + (str(key),))

    def __setitem__(self, key, value):
        self._check_public_caller()
        self._assign(self._prefix + (str(key),), value)

    def __delitem__(self, key):
        self._check_public_caller()
        self._delete(self._prefix + (str(key),))

    def _check_public_caller(self):
        if self._controller.is_owner():
            from .errors import ReentrantContextError
            raise ReentrantContextError('Public API called from the controller')

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
        if path == ("mounts",): raise AttributeError("mounts is reserved for the Context mount API")
        if path in self._graph.nodes:
            node = self._graph.nodes[path]
            if node.kind == "cell":
                if self._is_bound_source(value):
                    self._add_endpoint_edge(value, self._cell_endpoint(path), detach=True)
                elif isinstance(value, (Cell, PreparedCell)):
                    self._replace_cell_from_builder(path, value)
                elif callable(value):
                    raise NodeError("Cannot replace a cell node with transformer code")
                else:
                    self._cell_operation(path, (), value, detach=True)
            else:
                if self._is_bound_source(value):
                    self._replace_edges_to(path)
                    self._create_cell(path)
                    self._add_endpoint_edge(value, self._cell_endpoint(path))
                elif isinstance(value, (Transformer, PreparedTransformer)):
                    self._replace_transformer_from_builder(path, value)
                elif callable(value) or isinstance(value, (str, TransformerConfig)):
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
        elif isinstance(value, (Cell, PreparedCell)):
            self._create_cell_from_builder(path, value)
        elif isinstance(value, (Transformer, PreparedTransformer)):
            self._create_transformer_from_builder(path, value)
        elif callable(value) or isinstance(value, TransformerConfig):
            self._create_transformer(path, value)
        else:
            self._create_cell(path)
            self._cell_operation(path, (), value, detach=True)
        self._derive_all()

    def _delete(self, path):
        path = tuple(path)
        if path in self._graph.nodes:
            self._delete_subtree(path)
            return
        if path in self._graph.namespaces or self._graph.has_prefix(path):
            self._delete_subtree(path)
            return
        try:
            node_path, local = self._graph.resolve_existing(path)
        except KeyError:
            return
        if self._graph.nodes[node_path].kind == "cell":
            self._cell_delete_path(node_path, local)
        elif len(local) == 1:
            self._delete_transformer_pin(node_path, local[0])

    def _delete_subtree(self, path):
        """Remove a node or namespace subtree and release each role once."""

        path = tuple(path)
        for node_path in self._graph.descendants(path):
            self._mount_detach(node_path, derive=False)
            for lease in self._mount_node_leaves.pop(node_path, ()): lease._release_refholds()
            node = self._graph.nodes.pop(node_path, None)
            if node is None:
                continue
            if node.current_checksum is not None:
                node.current_checksum.decref_refholder()
                node.current_checksum = None
            self._release_node_producers(node, node_path)
            self._release_code_checksum(node_path)
            records = list(self._runtime.superseded_runs.pop(node_path, ()))
            current = self._runtime.current_runs.pop(node_path, None)
            if current is not None: records.append(current)
            for record in records:
                if record.et is not None: self._effects.append(record.et.cancel)
        self._graph.namespaces = {
            namespace
            for namespace in self._graph.namespaces
            if namespace[: len(path)] != path
        }
        self._graph.edges = [
            edge
            for edge in self._graph.edges
            if edge.source[: len(path)] != path and edge.target[: len(path)] != path
        ]
        self._sync_module_refholds()
        self._sync_superseded_refholds()
        self._derive_all()

    def _create_cell(self, path, *, celltype="mixed"):
        if path in self._graph.nodes:
            raise NodeError(path)
        node = Node(kind="cell", cell_config=CellConfig(celltype=celltype, target_celltype=celltype))
        self._graph.nodes[path] = node
        return node

    def _create_cell_from_builder(self, path, cell):
        self._create_cell(path, celltype=cell.celltype)
        try:
            self._replace_cell_from_builder(path, cell)
        except Exception:
            self._delete_subtree(path)
            raise

    def _replace_cell_from_builder(self, path, cell):
        node = self._graph.nodes[path]
        if node.mount and (cell.celltype, cell.target_celltype) != (node.cell_config.celltype, node.cell_config.target_celltype):
            raise ValueError("Mounted celltype cannot change; unmount first")
        session = self._mount_sessions.get(path)
        input_ref = cell.input_ref
        new_config = CellConfig(
            cell.celltype,
            cell.target_celltype,
            cell.validator,
            cell.validator_language,
        )
        if isinstance(input_ref, (Cell, PreparedCell)):
            if isinstance(input_ref, Cell) and input_ref._workflow_backend is not None:
                self._add_endpoint_edge(input_ref, self._cell_endpoint(path))
            else:
                upstream = self._graph.first_free("cell")
                self._create_cell_from_builder(upstream, input_ref)
                self._add_edge(upstream, path)
        elif self._is_bound_source(input_ref):
            self._add_endpoint_edge(input_ref, self._cell_endpoint(path))
        elif input_ref is not None:
            # Serialize and acquire the replacement before mutating the
            # graph's semantic configuration or releasing the old producer.
            checksum = checksum_for_value(input_ref, cell.celltype)
            producer = self._retain_producer(checksum, cell.celltype)
            old_producer = node.cell_root_producer
            node.cell_config = new_config
            node.cell_root_producer = producer
            self._revisions[path] = self._revisions.get(path, 0) + 1
            if old_producer is not None:
                self._release_producer(old_producer, path)
            self._remove_edges_targeting(path, (), descendants=True)
        else:
            node.cell_config = new_config
            producer = node.cell_root_producer
            node.cell_root_producer = None
            if producer is not None:
                self._release_producer(producer, path)
            self._remove_edges_targeting(path, (), descendants=True)
        if node.cell_config is not new_config and input_ref is not None:
            node.cell_config = new_config
        if session: session.sense_error = None
        if not isinstance(cell, PreparedCell):
            object.__setattr__(cell, "_workflow_backend", BoundCellBackend(self, path))
            cell._release_refholds()

    def _transformer_config_from_snapshot(self, snapshot, *, direct=False):
        codebuf = snapshot.codebuf
        code_checksum = (
            codebuf if isinstance(codebuf, Checksum) else codebuf.get_checksum()
        )
        cfg = TransformerConfig(
            code=codebuf,
            code_checksum=code_checksum,
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
            schema=snapshot.schema, compilation=copy.deepcopy(snapshot.compilation),
            objects=copy.deepcopy(snapshot.objects), header=snapshot.header,
        )
        cfg.pins.update(snapshot.args)
        cfg.celltypes.setdefault("result", "mixed")
        from .configuration import fingerprint
        fingerprint(cfg)
        return cfg, snapshot

    def _transformer_config_from_code(self, code):
        if isinstance(code, TransformerConfig):
            return code
        if code is None:
            return TransformerConfig()
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
                optional_pins={
                    name
                    for name, parameter in signature.parameters.items()
                    if parameter.default is not inspect.Parameter.empty
                },
                meta={"local": False},
            )
        buf = Buffer(str(code), "text")
        return TransformerConfig(code=buf, code_checksum=buf.get_checksum(), language="text", meta={"local": False})

    def _create_transformer(self, path, code):
        cfg = code if isinstance(code, TransformerConfig) else self._transformer_config_from_code(code)
        self._graph.nodes[path] = Node(kind="transformer", transformer_config=cfg)
        self._retain_code_checksum(path, cfg.code_checksum)

    def _create_transformer_from_builder(self, path, transformer):
        try:
            cfg, snapshot = transformer.config, transformer.snapshot
            self._graph.nodes[path] = Node(kind="transformer", transformer_config=cfg)
            self._retain_code_checksum(path, cfg.code_checksum)
            for pin, value in snapshot.args.items():
                self._set_transformer_pin(path, pin, value)

        except Exception:
            self._delete_subtree(path)
            raise

    def _replace_transformer_from_builder(self, path, transformer):
        node = self._graph.nodes[path]
        cfg, snapshot = transformer.config, transformer.snapshot
        if cfg.signature_parameters() is None:
            cfg.pins.update(node.transformer_config.pins)
        removed_pins = node.transformer_config.pins - cfg.pins
        old_producers = node.transformer_pin_producers
        old_code_refholds = self._code_refholds.copy()
        old_module_refholds = self._module_refholds.copy()
        # Validate every replacement edge before acquiring/publishing configuration.
        for pin, value in snapshot.args.items():
            ep = self._endpoint(value)
            if ep is not None:
                source = self._source_path(value)
                source_node, _ = self._graph.resolve_existing(source)
                if source_node == path or self._would_cycle(source_node, path):
                    raise DependencyError("Dependency cycle")
                self._check_authority(path, (pin,))

        new_producers = {
            pin: producer for pin, producer in old_producers.items()
            if pin in cfg.pins and pin not in snapshot.args
        }
        staged_checksums = []
        published = False
        try:
            # Acquire all replacement state before touching the old semantic
            # fields.  This makes a failed conversion leave the old node live.
            for pin, value in snapshot.args.items():
                if self._endpoint(value) is not None:
                    continue
                checksum = checksum_for_value(value, cfg.celltypes.get(pin, "mixed"))
                new_producers[pin] = self._retain_producer(
                    checksum, cfg.celltypes.get(pin, "mixed")
                )
                staged_checksums.append(checksum)

            new_code_checksum = normalize_checksum(cfg.code_checksum)
            new_code_checksum.incref_refholder()
            staged_checksums.append(new_code_checksum)

            new_module_refs = {}
            for module_name, module in cfg.modules.items():
                if not isinstance(module, Checksum):
                    continue
                checksum = normalize_checksum(module)
                checksum.incref_refholder()
                staged_checksums.append(checksum)
                new_module_refs[(path, module_name)] = checksum

            # Publish the fully acquired replacement as one semantic state.
            node.transformer_config = cfg
            node.transformer_pin_producers = new_producers
            self._code_refholds[path] = new_code_checksum
            self._module_refholds = {
                role: checksum
                for role, checksum in self._module_refholds.items()
                if role[0] != path
            }
            self._module_refholds.update(new_module_refs)
            published = True
            staged_checksums.clear()

            for pin, producer in old_producers.items():
                if new_producers.get(pin) is not producer:
                    self._release_producer(producer, path + (pin,))
            for pin in removed_pins | set(snapshot.args):
                self._remove_edges_targeting(path, (pin,), descendants=True)
            self._remove_edges_targeting(path, ("code",), descendants=True)
            self._revisions[path] = self._revisions.get(path, 0) + 1
            old_code_checksum = old_code_refholds.get(path)
            if old_code_checksum is not None:
                old_code_checksum.decref_refholder()
            for role, checksum in old_module_refholds.items():
                if role[0] == path:
                    checksum.decref_refholder()

            for pin, value in snapshot.args.items():
                if self._endpoint(value) is not None:
                    self._set_transformer_pin(path, pin, value)
            self._derive_all()

        except Exception as exc:
            # Failed staging never touched live state: the request is rejected.
            # Published state is never rolled back (MOD-5); a failure after
            # publication is controller-internal and poisons the Context (§12.3).
            for checksum in reversed(staged_checksums):
                checksum.decref_refholder()
            if published:
                self._controller.poison(self, "_replace_transformer_from_builder", exc)
            raise

    def _set_cell_root(self, path, checksum, celltype):
        self._set_cell_root_with_edges(path, checksum, celltype, clear_edges=True)

    def _set_cell_root_with_edges(self, path, checksum, celltype, *, clear_edges):
        session = self._mount_sessions.get(path)
        if session: session.sense_error = None
        if checksum is None:
            for lease in self._mount_node_leaves.pop(path, ()): lease._release_refholds()
        node = self._graph.nodes[path]
        producer = None if checksum is None else self._retain_producer(checksum, celltype)
        old_producer = node.cell_root_producer
        node.cell_root_producer = producer
        self._revisions[path] = self._revisions.get(path, 0) + 1
        if old_producer is not None:
            self._release_producer(old_producer, path)
        if clear_edges:
            self._remove_edges_targeting(path, (), descendants=True)

    def _set_cell_value(self, path, local, value):
        self._cell_operation(path, local, value)

    def _set_cell_checksum(self, path, local, checksum, *, celltype=None):
        self._cell_operation(path, local, Checksum(checksum), checksum_rhs=True)

    def _cell_operation(self, node_path, local, value, *, checksum_rhs=False, detach=False):
        node = self._graph.nodes[node_path]
        local = tuple(local)
        endpoint = self._endpoint(value)
        if endpoint is not None:
            self._add_endpoint_edge(value, self._cell_endpoint(node_path, local), detach=detach)
        else:
            self._validate_write(node_path, local, detach)
            if local:
                raise RuntimeError("Sub-path values must be prepared outside the controller")
            from seamless.checksum.hash_type_validation import validate_deserializable_as
            if value is not None:
                validate_deserializable_as(value, Buffer._map_celltype(node.cell_config.celltype))
            self._set_cell_root_with_edges(node_path, value, node.cell_config.celltype, clear_edges=detach)
        self._derive_all()

    def _validate_write(self, node_path, local, detach):
        for target in self._incoming_for(node_path):
            ancestor = len(target) < len(local) and local[:len(target)] == target
            covered = target[:len(local)] == local
            if ancestor or (covered and not detach):
                raise AuthorityError(f"Cell {node_path!r}{local!r} is controlled by an incoming edge")

    def _update_base(self, node_path, local, detach):
        self._validate_write(node_path, local, detach)
        node = self._graph.nodes[node_path]
        return (Lease(node.current_checksum, node.cell_config.celltype),
                self._revisions.get(node_path, 0), node.state,
                tuple(self._incoming_for(node_path)))

    def _commit_update(self, node_path, local, checksum, base, revision, detach):
        self._validate_write(node_path, local, detach)
        node = self._graph.nodes[node_path]
        current = node.current_checksum.hex() if node.current_checksum is not None else None
        if current != base or self._revisions.get(node_path, 0) != revision:
            return False
        self._set_cell_root_with_edges(node_path, checksum, node.cell_config.celltype, clear_edges=False)
        if detach: self._remove_edges_targeting(node_path, local, descendants=True)
        self._derive_all()
        return True

    def _cell_delete_path(self, node_path, local):
        local = tuple(local)
        if not local:
            node = self._graph.nodes[node_path]
            producer = node.cell_root_producer
            node.cell_root_producer = None
            if producer is not None:
                self._release_producer(producer, node_path)
            self._remove_edges_targeting(node_path, (), descendants=True)
            self._derive_all()
            return
        raise RuntimeError("Sub-path deletes must be prepared outside the controller")

    def _set_transformer_code(self, node_path, code):
        endpoint = self._endpoint(code)
        if endpoint is not None:
            self._add_endpoint_edge(code, self._transformer_endpoint(node_path, "code"))
            self._release_code_checksum(node_path)
            self._graph.nodes[node_path].transformer_config.code_checksum = None
            self._derive_all()
            return
        node = self._graph.nodes[node_path]
        removed_pins = node.transformer_config.pins - code.pins
        node.transformer_config = code
        for pin in removed_pins:
            producer = node.transformer_pin_producers.pop(pin, None)
            if producer is not None:
                self._release_producer(producer, node_path + (pin,))
            self._remove_edges_targeting(node_path, (pin,), descendants=True)
        self._remove_edges_targeting(node_path, ("code",), descendants=True)
        self._revisions[node_path] = self._revisions.get(node_path, 0) + 1
        self._replace_code_checksum(node_path, code.code_checksum)
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
            self._add_endpoint_edge(value, self._transformer_endpoint(node_path, pin), detach=True)
        else:
            self._check_authority(node_path, (pin,))
            checksum = checksum_for_value(value, cfg.celltypes.get(pin, "mixed"))
            producer = self._retain_producer(
                checksum, cfg.celltypes.get(pin, "mixed")
            )
            node = self._graph.nodes[node_path]
            old_producer = node.transformer_pin_producers.get(pin)
            node.transformer_pin_producers[pin] = producer
            if old_producer is not None:
                self._release_producer(old_producer, node_path + (pin,))
            self._remove_edges_targeting(node_path, (pin,), descendants=True)
        self._derive_all()

    def _retain_code_checksum(self, path, checksum):
        if checksum is None:
            return
        checksum = normalize_checksum(checksum)
        checksum.incref_refholder()
        self._code_refholds[tuple(path)] = checksum

    def _replace_code_checksum(self, path, checksum):
        old = self._code_refholds.get(tuple(path))
        new = None if checksum is None else normalize_checksum(checksum)
        if new is not None:
            new.incref_refholder()
            self._code_refholds[tuple(path)] = new
        else:
            self._code_refholds.pop(tuple(path), None)
        if old is not None:
            old.decref_refholder()

    def _release_code_checksum(self, path):
        old = self._code_refholds.pop(tuple(path), None)
        if old is not None:
            old.decref_refholder()

    def _sync_module_refholds(self):
        active = {}
        for path, node in self._graph.nodes.items():
            if node.kind != "transformer":
                continue
            for module_name, module in node.transformer_config.modules.items():
                if isinstance(module, Checksum):
                    active[(path, module_name)] = normalize_checksum(module)
        old_refholds = self._module_refholds
        acquired = []
        try:
            for role, checksum in active.items():
                old = old_refholds.get(role)
                if old is None or old != checksum:
                    checksum.incref_refholder()
                    acquired.append(checksum)
        except Exception:
            for checksum in reversed(acquired):
                checksum.decref_refholder()
            raise

        # Publish the new operational map only after every new role is held,
        # then release roles that are absent or replaced.  Overwriting the map
        # first loses the old checksum and strands its refholder reference.
        self._module_refholds = active
        for role, checksum in old_refholds.items():
            if active.get(role) != checksum:
                checksum.decref_refholder()

    def _delete_transformer_pin(self, node_path, pin):
        if not pin:
            raise PathError("Transformer pin name must be non-empty")
        producer = self._graph.nodes[node_path].transformer_pin_producers.pop(
            pin, None
        )
        if producer is not None:
            self._release_producer(producer, node_path + (pin,))
        self._remove_edges_targeting(node_path, (pin,), descendants=True)
        self._derive_all()

    def _endpoint(self, value):
        if isinstance(value, BoundEndpoint):
            return value
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

    def _add_endpoint_edge(self, source, target, *, detach=False):
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
        elif target.endpoint_kind == "transformer-input":
            if len(target.local_path) != 1:
                raise PathError("Transformer targets must address a whole pin")
        self._add_edge(source_ep.node_path + source_ep.local_path, target.node_path + target.local_path, detach=detach)
        target_node = self._graph.nodes[target.node_path]
        if target_node.kind == "cell" and not target.local_path:
            producer = target_node.cell_root_producer
            target_node.cell_root_producer = None
            if producer is not None:
                self._release_producer(producer, target.node_path)
        elif target_node.kind == "transformer" and len(target.local_path) == 1:
            pin = target.local_path[0]
            producer = target_node.transformer_pin_producers.pop(pin, None)
            if producer is not None:
                self._release_producer(producer, target.node_path + (pin,))

    def _add_edge(self, source, target, *, detach=False):
        source_node, _ = self._graph.resolve_existing(tuple(source))
        target_node, target_local = self._graph.resolve_existing(tuple(target))
        mount = self._graph.nodes[target_node].mount
        if mount and "r" in mount.mode:
            raise AuthorityError("Sensing mount is the producer; unmount first")
        if source_node == target_node:
            raise DependencyError("Self-dependencies are not supported")
        if self._would_cycle(source_node, target_node):
            raise DependencyError("Dependency cycle")
        if detach:
            self._validate_write(target_node, target_local, True)
        else:
            self._check_authority(target_node, target_local)
        self._remove_edges_targeting(target_node, target_local, descendants=True)
        self._graph.edges.append(Edge(tuple(source), tuple(target)))
        self._derive_all()

    def _would_cycle(self, source, target):
        seen = set()
        stack = [target]
        while stack:
            current = stack.pop()
            if current == source:
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
        return producer.checksum

    def _retain_producer(self, checksum, celltype):
        checksum = normalize_checksum(checksum)
        checksum.incref_refholder()
        return ConstantProducer(checksum, celltype)

    def _release_producer(self, producer, owner):
        checksum = producer.checksum
        checksum.decref_refholder()

    def _replace_current_checksum(self, node_path, checksum):
        """Acquire a node current result before replacing its semantic field."""

        node = self._graph.nodes[node_path]
        new_checksum = None if checksum is None else normalize_checksum(checksum)
        old_checksum = node.current_checksum
        if new_checksum is not None:
            new_checksum.incref_refholder()
        node.current_checksum = new_checksum
        if old_checksum is not None:
            old_checksum.decref_refholder()

    def _refheld_checksums(self):
        if self._refholds_released:
            return ()
        claims = []
        for path, node in self._graph.nodes.items():
            if node.cell_root_producer is not None:
                claims.append((node.cell_root_producer.checksum, f"cell:{':'.join(path)}:literal"))
            for pin, producer in node.transformer_pin_producers.items():
                claims.append((producer.checksum, f"transformer:{':'.join(path)}:pin:{pin}"))
            if node.kind == "transformer":
                cfg = node.transformer_config
                code_checksum = cfg.code_checksum
                if code_checksum is not None:
                    claims.append((code_checksum, f"transformer:{':'.join(path)}:code"))
                for module_name, module in cfg.modules.items():
                    if isinstance(module, Checksum):
                        claims.append((module, f"transformer:{':'.join(path)}:module:{module_name}"))
            if node.current_checksum is not None:
                claims.append((node.current_checksum, f"node:{':'.join(path)}:current"))
        for path, records in self._runtime.superseded_runs.items():
            for record in records:
                if record.result_checksum is not None and record.phase != "cancelled":
                    claims.append((record.result_checksum, f"node:{':'.join(path)}:superseded:{record.generation}"))
        return claims

    def _release_refholds(self):
        if self._refholds_released:
            return
        for checksum, _role in list(self._refheld_checksums()):
            checksum.decref_refholder()
        self._superseded_refholds.clear()
        self._refholds_released = True

    def _release_all_producers(self):
        self._release_refholds()

    def _release_node_producers(self, node, path):
        if node.cell_root_producer is not None:
            self._release_producer(node.cell_root_producer, path)
        for pin, producer in node.transformer_pin_producers.items():
            self._release_producer(producer, path + (pin,))

    def _derive_all(self):
        # Derivation runs after a turn has published its graph change, so a
        # failure here leaves derived state inconsistent with the graph (§12.3).
        try:
            self._derive_graph()
        except Exception as exc:
            self._controller.poison(self, "_derive_all", exc)
            raise

    def _derive_graph(self):
        # A source may sort after its target; converge the small durable graph
        # rather than making public state depend on lexical node names.
        self._sync_module_refholds()
        self._used_facts = set()
        visited, order = set(), []
        def visit(path):
            if path in visited: return
            visited.add(path)
            for edge in self._incoming_for(path).values():
                source, _ = self._graph.resolve_existing(edge.source)
                visit(source)
            order.append(path)
        for path in sorted(self._graph.nodes): visit(path)
        for path in order: self._derive_node(path)
        for key in set(self._jobs) - self._used_facts:
            task = self._jobs.pop(key)
            if task is not None: self._effects.append(task.cancel)
        for key in set(self._facts) - self._used_facts:
            lease, _ = self._facts.pop(key)
            if lease is not None: lease._release_refholds()
        for path in sorted(self._graph.nodes):
            self._update_runtime(path)
        self._sync_superseded_refholds()

    def _sync_superseded_refholds(self):
        active = {}
        for records in self._runtime.superseded_runs.values():
            for record in records:
                if record.phase != "cancelled" and record.result_checksum is not None:
                    active[id(record)] = record.result_checksum
        for record_id, checksum in list(self._superseded_refholds.items()):
            if record_id not in active:
                checksum.decref_refholder()
                del self._superseded_refholds[record_id]
        for record_id, checksum in active.items():
            if record_id not in self._superseded_refholds:
                checksum.incref_refholder()
                self._superseded_refholds[record_id] = checksum

    def _derive_node(self, path):
        node = self._graph.nodes[path]
        if node.kind == "cell":
            self._derive_cell(path, node)
        else:
            self._derive_transformer(path, node)

    def _derive_cell(self, path, node):
        session = self._mount_sessions.get(path)
        if session and session.sense_error:
            self._replace_current_checksum(path, None)
            node.state, node.block_reason, node.exception = "failed", None, session.sense_error
            return
        incoming = self._incoming_for(path)
        cfg = node.cell_config
        if () in incoming:
            edge = incoming[()]
            state, checksum = self._source_state(edge)
            if state != "complete":
                self._apply_upstream_state(node, (state, checksum))
                return
            source_path, _ = self._graph.resolve_existing(edge.source)
            source_type = self._node_celltype(source_path)
            if source_type != cfg.celltype or cfg.target_celltype != cfg.celltype or cfg.validator is not None:
                state, checksum = self._projection(checksum, (), source_type, cfg.target_celltype, cfg.validator, cfg.validator_language)
            self._apply_upstream_state(node, (state, checksum))
            return
        producer = node.cell_root_producer
        if not incoming:
            checksum = producer.checksum if producer else None
            self._replace_current_checksum(path, checksum)
            node.state, node.block_reason, node.exception = ("complete" if checksum is not None else "unwired"), None, None
            return
        inputs = []
        for local, edge in incoming.items():
            state, checksum = self._source_state(edge)
            if state != "complete":
                self._apply_pending(node, [edge])
                return
            source, _ = self._graph.resolve_existing(edge.source)
            inputs.append((local, checksum, self._node_celltype(source)))
        root = producer.checksum if producer else None
        root_type = producer.celltype if producer else cfg.celltype
        key = ("merge", root.hex() if root is not None else None, root_type,
               tuple((local, cs.hex(), ct) for local, cs, ct in inputs), cfg.celltype)
        state, checksum, error = self._demand(key, evaluate_cell, (root, root_type, tuple(inputs), cfg.celltype),
                                            [cs for _, cs, _ in inputs] + ([root] if root is not None else []))
        self._replace_current_checksum(path, checksum)
        node.state, node.block_reason, node.exception = state, None, error

    def _node_celltype(self, path):
        node = self._graph.nodes[path]
        return node.cell_config.celltype if node.kind == "cell" else node.transformer_config.celltypes.get("result", "mixed")

    def _projection(self, checksum, local, celltype, target_type=None, validator=None, validator_language=None):
        target_type = target_type or celltype
        # Python slices are represented as strings in the content key.
        key = ("expression", checksum.hex(), _path_string(local), celltype, target_type,
               validator.hex() if isinstance(validator, Checksum) else validator, validator_language)
        state, result, error = self._demand(key, evaluate_projection,
            (checksum, tuple(local), celltype, target_type, validator, validator_language), [checksum])
        return state, result

    def _demand(self, key, function, args, checksums):
        self._used_facts.add(key)
        fact = self._facts.get(key)
        if fact is not None:
            lease, error = fact
            return ("failed" if error else "complete"), lease.checksum if lease else None, error
        if key not in self._jobs:
            leases = tuple(Lease(cs) for cs in checksums)
            self._jobs[key] = None
            import weakref
            owner = weakref.ref(self)
            execution = self._expression_execution
            async def work(leases=leases):
                try:
                    if function is evaluate_projection:
                        from seamless.checksum.expression import evaluate_expression_remote, cancel_expression
                        cs, local, ct, target, validator, validator_language = args
                        try:
                            checksum = await evaluate_expression_remote(cs, _path_string(local), ct, target,
                                validator=validator, validator_language=validator_language,
                                execution=execution, member_id=key)
                            if checksum is None: raise KeyError(_path_string(local))
                        except asyncio.CancelledError:
                            cancel_expression(cs, _path_string(local), ct, target, member_id=key)
                            raise
                    else:
                        worker_leases = tuple(Lease(lease.checksum) for lease in leases)
                        def evaluate(worker_leases=worker_leases):
                            try:
                                return function(*args)
                            finally:
                                for lease in worker_leases: lease._release_refholds()
                        checksum = await asyncio.to_thread(evaluate)
                    payload, error = Lease(checksum), None
                except Exception as exc:
                    from .errors import WorkflowExecutionError
                    payload, error = None, WorkflowExecutionError(str(exc))
                finally:
                    for lease in leases: lease._release_refholds()
                context = owner()
                if context is not None and context._controller.accepting:
                    try:
                        context._controller.submit("_accept_fact", (key, payload, error), klass=5)
                    except Exception:
                        if payload is not None: payload._release_refholds()
                elif payload is not None:
                    payload._release_refholds()
            def launch():
                self._jobs[key] = self._side.submit(work())
            self._effects.append(launch)
        return "waiting", None, None

    def _accept_fact(self, key, lease, error):
        if self._closing or key not in self._jobs:
            if lease is not None: lease._release_refholds()
            return
        self._facts[key] = (lease, error)
        self._jobs.pop(key, None)
        self._derive_all()

    def _source_state(self, edge):
        source_node, source_local = self._graph.resolve_existing(edge.source)
        node = self._graph.nodes[source_node]
        if node.state != "complete":
            return ("failed" if node.block_reason == "blocked-by-error" else node.state), None
        if source_local:
            return self._projection(node.current_checksum, source_local, self._node_celltype(source_node))
        return "complete", node.current_checksum

    def _apply_pending(self, node, edges):
        states = [self._source_state(edge)[0] for edge in edges]
        if "failed" in states:
            node.state, node.block_reason = "blocked", "blocked-by-error"
        elif any(state in {"blocked", "unwired"} for state in states):
            node.state, node.block_reason = "blocked", "blocked-by-unwired"
        else:
            node.state, node.block_reason = "waiting", None
        self._replace_current_checksum(
            next(path for path, candidate in self._graph.nodes.items() if candidate is node),
            None,
        )

    def _apply_upstream_state(self, node, upstream):
        state, checksum = upstream
        if state == "complete":
            self._replace_current_checksum(
                next(path for path, candidate in self._graph.nodes.items() if candidate is node),
                checksum,
            )
            node.state, node.block_reason, node.exception = "complete", None, None
        elif state == "failed":
            self._replace_current_checksum(next(path for path, candidate in self._graph.nodes.items() if candidate is node), None)
            node.state, node.block_reason = "blocked", "blocked-by-error"
        elif state in {"blocked", "unwired"}:
            self._replace_current_checksum(next(path for path, candidate in self._graph.nodes.items() if candidate is node), None)
            node.state, node.block_reason = "blocked", "blocked-by-unwired"
        else:
            self._replace_current_checksum(next(path for path, candidate in self._graph.nodes.items() if candidate is node), None)
            node.state, node.block_reason = "waiting", None

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
        return self._projection(node.current_checksum, local, node.cell_config.celltype)[1]

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
        if node.state == "unwired": raise NodeError("Node is unwired")
        if node.state == "blocked": raise NodeError(f"Node is blocked: {node.block_reason}")
        if node.state == "failed": raise node.exception
        return node.current_checksum if checksum else self._get_value(node_path, ())

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
        checksum = self._get_checksum(endpoint.node_path, ())
        if checksum is None:
            raise ValueError("Workflow source has no concrete checksum")
        return Expression(checksum, path=_path_string(endpoint.local_path),
                          celltype=self._node_celltype(endpoint.node_path),
                          target_celltype=self._node_celltype(endpoint.node_path))

    def _public_source(self, source):
        node_path, local = self._graph.resolve_existing(source)
        node = self._graph.nodes[node_path]
        if node.kind == "cell":
            return Cell._from_backend(BoundCellBackend(self, node_path, local))
        return Cell._from_backend(BoundCellBackend(self, node_path, local, readonly=True))

    def _clear_exception(self, node_path):
        session = self._mount_sessions.get(node_path)
        if session and session.sense_error:
            self._effects.append(lambda: session.registration.service.poll(session.registration, force=True))
            return
        node = self._graph.nodes[node_path]
        if node.exception is None: return
        current = self._runtime.current_runs.pop(node_path, None)
        if current is not None and current.et is not None: self._effects.append(current.et.cancel)
        node.exception = None
        self._derive_all()

    def prune(self, node_path=None):
        paths = None if node_path is None else {node_path} | self._downstream_cone(node_path)
        for path, records in self._runtime.superseded_runs.items():
            if paths is None or path in paths:
                for record in records:
                    if record.et is not None: self._effects.append(record.et.cancel)
        result = {"cancelled": self._runtime.prune(paths)}
        self._sync_superseded_refholds()
        return result

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
        if node.kind == "transformer": return
        identity = (path, node.current_checksum.hex() if node.current_checksum else None, node.state)
        current = self._runtime.current_runs.get(path)
        identity_checksum = node.current_checksum
        if identity_checksum is None:
            if current is not None: self._runtime.supersede(path)
            return
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
                entry = {"type": "cell", "path": list(path), "celltype": node.cell_config.celltype, "target_celltype": node.cell_config.target_celltype, "validator": node.cell_config.validator, "validator_language": node.cell_config.validator_language, "value": None if node.cell_root_producer is None else {"checksum": node.cell_root_producer.checksum.hex(), "celltype": node.cell_root_producer.celltype}}
            else:
                cfg = node.transformer_config
                entry = {"type": "transformer", "path": list(path), "language": cfg.language, "result_celltype": cfg.celltypes.get("result", "mixed"), "schema": cfg.schema, "compilation": copy.deepcopy(cfg.compilation), "objects": copy.deepcopy(cfg.objects), "header": cfg.header, "call_mode": cfg.call_mode, "pins": {p: {"celltype": cfg.celltypes.get(p, "mixed")} for p in sorted(cfg.pins)}, "optional_pins": sorted(cfg.optional_pins), "checksum": {"code": cfg.code_checksum.hex() if cfg.code_checksum else None}, "code": cfg.code if hasattr(cfg.code, "decode") else None, "meta": copy.deepcopy(cfg.meta), "modules": copy.deepcopy(cfg.modules), "globals": copy.deepcopy(cfg.globals), "environment": copy.deepcopy(cfg.environment), "scratch": cfg.scratch, "local": cfg.local, "direct_print": cfg.direct_print, "producers": {p: {"checksum": q.checksum.hex(), "celltype": q.celltype} for p, q in sorted(node.transformer_pin_producers.items())}}
            if node.mount is not None and node.mount.driver == "file": entry["mount"] = node.mount.to_graph()
            if runtime:
                entry["runtime"] = {"state": node.state, "block_reason": node.block_reason, "checksum": node.current_checksum.hex() if node.current_checksum else None, "exception": type(node.exception).__name__ if node.exception else None, "run": self._runtime_graph_entry(path)}
            nodes.append(entry)
        return {"__seamless_workflow__": "0.3", "nodes": nodes, "connections": [{"type": "connection", "source": list(e.source), "target": list(e.target)} for e in sorted(self._graph.edges, key=lambda e: (e.source, e.target))], "params": {}}

    def set_graph(self, graph, *, mount_prepared=()):
        # Acquire the staged durable graph before releasing any live roles.
        claims = []
        for path, node in graph.nodes.items():
            if node.cell_root_producer is not None: claims.append(node.cell_root_producer.checksum)
            claims.extend(p.checksum for p in node.transformer_pin_producers.values())
            if node.kind == "transformer":
                cfg = node.transformer_config
                if cfg.code_checksum is not None: claims.append(cfg.code_checksum)
                claims.extend(v for v in cfg.modules.values() if isinstance(v, Checksum))
        acquired = []
        try:
            for cs in claims:
                cs.incref_refholder()
                acquired.append(cs)
        except Exception:
            for cs in acquired: cs.decref_refholder()
            raise
        for record in list(self._runtime.current_runs.values()) + [r for q in self._runtime.superseded_runs.values() for r in q]:
            if record.et is not None: self._effects.append(record.et.cancel)
        for path in tuple(self._mount_sessions): self._mount_detach(path, delete=False, derive=False)
        for path, leases in tuple(self._mount_node_leaves.items()):
            old, new = self._graph.nodes.get(path), graph.nodes.get(path)
            if (old is None or new is None or old.cell_root_producer is None or new.cell_root_producer is None
                    or old.cell_root_producer.checksum != new.cell_root_producer.checksum):
                for lease in leases: lease._release_refholds()
                self._mount_node_leaves.pop(path)
        self._release_refholds()
        self._graph = graph
        # Do not reuse generations across graph replacement: late completions
        # from the old graph must never match a new run at the same path.
        self._runtime = ContextRuntime(generation=self._runtime.generation)
        self._refholds_released = False
        self._code_refholds = {p:n.transformer_config.code_checksum for p,n in graph.nodes.items()
                              if n.kind == "transformer" and n.transformer_config.code_checksum is not None}
        self._module_refholds = {(p,k):v for p,n in graph.nodes.items() if n.kind == "transformer"
                                for k,v in n.transformer_config.modules.items() if isinstance(v, Checksum)}
        self._superseded_refholds = {}
        self._revisions = {p:self._revisions.get(p,0)+1 for p in graph.nodes}
        self._derive_all()
        return [self._mount_attach(*prepared) for prepared in mount_prepared]

    def _copy_subcontext(self, source_prefix, target_prefix):
        self._graph.namespaces.add(target_prefix)
        for path in self._graph.descendants(source_prefix):
            copied_path = target_prefix + path[len(source_prefix):]
            copied_node = copy.deepcopy(self._graph.nodes[path])
            self._graph.nodes[copied_path] = copied_node
            if copied_node.cell_root_producer is not None:
                producer = copied_node.cell_root_producer
                copied_node.cell_root_producer = self._retain_producer(
                    producer.checksum, producer.celltype
                )
            for pin, producer in list(copied_node.transformer_pin_producers.items()):
                copied_node.transformer_pin_producers[pin] = self._retain_producer(
                    producer.checksum, producer.celltype
                )
            if copied_node.current_checksum is not None:
                copied_node.current_checksum.incref_refholder()
            if copied_node.kind == "transformer":
                self._retain_code_checksum(copied_path, copied_node.transformer_config.code_checksum)
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



from .ingress import controller_method, _wait, _wait_async


def _compute(self, timeout=None):
    return _wait(self, barrier=True, timeout=timeout)


async def _computation(self, timeout=None):
    return await _wait_async(self, barrier=True, timeout=timeout)


for _name, _method in {**vars(AttachmentRuntime), **vars(Reactive), **vars(RuntimeAPI), **vars(Context)}.items():
    if callable(_method) and not _name.startswith("__") and _name not in {
        "close", "_after_turn", "_check_public_caller", "_transformer_config_from_code", "_transformer_config_from_snapshot"
    }:
        setattr(Context, _name, controller_method(_method))
Context.compute = _compute
Context.computation = _computation
