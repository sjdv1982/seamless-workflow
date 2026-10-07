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

from seamless import Buffer, CacheMissError, Cell, Checksum, Expression
from seamless_transformer.frozen_transformer import FrozenTransformer

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
from .graph import (
    CellConfig,
    ConstantProducer,
    ContextGraph,
    Edge,
    Node,
    NodePath,
    TransformerConfig,
    anonymous_links,
    fusible_runs,
    deep_barrier as _deep_barrier,
    deep_recipe_error as _deep_recipe_error,
    projected_celltype,
    split_deep_step,
)
from .scheduler import ContextRuntime, ExceptionInfo, RunRecord
from .views import MissingView, SubContextView

from .sidework import Lease, PreparedCell, PreparedTransformer, SideLoop, evaluate_cell, evaluate_projection

PIN_CELLTYPES = {"plain", "mixed", "deepcell", "deepfolder", "folder"}
_DEEP_CELLTYPES = {"deepcell", "deepfolder", "folder"}


def _projected_source_type_error(
    source_ep, source_type, target_type, target_path, target_pin=None, target_local=(),
    root_type=None,
):
    """The wiring rule's refusal; its text is contract (cells.md, *Connecting*).

    The header ``would convert <in> -> <out> behind a projection.`` is followed
    by the two spellings, projection first, each glossed ``# item k of ...``.
    The source is spelled as written: its node, then its links in order --
    bracket-form path steps and ``as_celltype`` conversions -- and ``k`` is
    the path step(s) after its last conversion, which are what the target
    would convert behind.  ``root_type`` is the celltype of the source node.
    A conversion-first spelling that the conversion table refuses -- a deep
    index converted outside the deep table (deep-celltypes.md, *Bound wiring
    around the step*) -- is not offered.
    """
    import json
    from seamless.checksum.expression import validate_expression_shape

    source_name = "ctx." + ".".join(source_ep.node_path)
    if source_ep.endpoint_kind == "transformer-result":
        source_name += ".result"
    target_name = "ctx." + ".".join(target_path)
    if target_local:
        component = target_local[0]
        if isinstance(component, str):
            target_name += f"[{json.dumps(component)}]"
        else:
            target_name += f"[{component}]"
    if target_pin is not None:
        target_name += f".pins.{target_pin}"

    path = tuple(source_ep.local_path)
    steps = tuple(source_ep.conversion_steps)
    if source_ep.conversion and not steps:
        position = 0 if source_ep.conversion_before else len(path)
        steps = ((position, source_ep.celltype),)
    base, current, position = source_name, root_type or source_type, 0
    for step_position, converted in sorted(steps, key=lambda step: step[0]):
        step_position = min(max(step_position, position), len(path))
        base += _spelled_path(path[position:step_position]) + f'.as_celltype("{converted}")'
        current, position = converted, step_position
    trailing = path[position:]
    spelled = _spelled_path(trailing)
    item = spelled[1:-1] if len(trailing) == 1 else spelled

    if current in _DEEP_CELLTYPES:
        nature = " (a member)"
    elif current in _CHARACTER_CELLTYPES:
        nature = " (a character)"
    else:
        nature = ""
    spellings = [(
        f'{target_name} = {base}{spelled}.as_celltype("{target_type}")',
        f"# item {item} of the {current}{nature}, as {target_type}"
        if trailing else f"# the {current}, as {target_type}",
    )]
    try:
        validate_expression_shape("", current, target_type)
        conversion_legal = bool(trailing)
    except (TypeError, ValueError):
        conversion_legal = False
    if conversion_legal:
        if current in _CHARACTER_CELLTYPES:
            parsed = "the parsed mapping" if isinstance(trailing[0], str) else "the parsed list"
        else:
            parsed = f"the {target_type}"
        spellings.append((
            f'{target_name} = {base}.as_celltype("{target_type}"){spelled}',
            f"# item {item} of {parsed}",
        ))
    width = max(len(code) for code, _gloss in spellings)
    lines = [f"would convert {source_type} -> {target_type} behind a projection."]
    lines += [f"  {code.ljust(width)}   {gloss}" for code, gloss in spellings]
    return TypeError("\n".join(lines))


# Celltypes whose value is a string, so that one item of it is a character.
_CHARACTER_CELLTYPES = frozenset({"text", "str", "python", "ipython", "cson", "yaml", "checksum"})


def _spelled_path(path):
    """A projection path as it is written on a handle: ``["k"]``, ``[3]``, ``[1:3]``.

    String keys are written in bracket form, since deep keys routinely contain
    ``/`` and ``.`` (deep-celltypes.md, *Paths*).
    """
    import json

    spelled = []
    for component in path:
        if isinstance(component, slice):
            def bound(value):
                return "" if value is None else repr(value)

            text = f"{bound(component.start)}:{bound(component.stop)}"
            if component.step is not None:
                text += f":{component.step!r}"
            spelled.append(f"[{text}]")
        elif isinstance(component, str):
            spelled.append(f"[{json.dumps(component)}]")
        else:
            spelled.append(f"[{component!r}]")
    return "".join(spelled)



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
        object.__setattr__(self, "_anonymous_current_refholds", {})
        object.__setattr__(self, "_edge_pin_refholds", {})
        object.__setattr__(self, "_edge_code_states", {})
        object.__setattr__(self, "_anonymous_current_updates", {})
        object.__setattr__(self, "_prefix", ())
        from seamless.reference_lifecycle import register_refholder

        from .controller import Controller
        object.__setattr__(self, "_controller", Controller(self))
        object.__setattr__(self, "_side", SideLoop(f"Context-side-{self.top_id[:8]}"))
        object.__setattr__(self, "_jobs", {})
        object.__setattr__(self, "_facts", {})
        object.__setattr__(self, "_barriers", {})
        object.__setattr__(self, "_effects", [])
        object.__setattr__(self, "_edge_errors", {})
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
            if node.mount and cfg.celltype != node.cell_config.celltype:
                raise ValueError("Mounted celltype cannot change; unmount first")
            edge = self._incoming_edge(path, ())
            if edge is not None:
                source_path, source_local = self._graph.resolve_existing(edge.source)
                if (source_local and not edge.source_conversion
                        and not edge.source_conversion_steps
                        and cfg.celltype != self._node_celltype(source_path)):
                    raise TypeError(
                        "Cannot implicitly convert behind a projection; use as_celltype() "
                        "before or after projecting"
                    )
            node.cell_config = cfg
        else:
            for pin, celltype in cfg.celltypes.items():
                if celltype == node.transformer_config.celltypes.get(pin):
                    continue
                edge = self._incoming_edge(path, (pin,))
                if edge is not None:
                    source_path, local = self._graph.resolve_existing(edge.source)
                    links = anonymous_links(
                        self._node_celltype(source_path), local,
                        edge.source_conversion_steps,
                    )
                    source_type, projected_path = links[-1] if links else (
                        self._node_celltype(source_path), (),
                    )
                    if projected_path and source_type != celltype:
                        raise TypeError("Cannot implicitly convert behind a projection; use as_celltype() before or after projecting")
            if cfg.compilation is not None:
                removed_pins = node.transformer_config.pins - cfg.pins
                for pin in removed_pins:
                    producer = node.transformer_pin_producers.pop(pin, None)
                    if producer is not None:
                        self._release_producer(producer, path + (pin,))
                    self._remove_edges_targeting(path, (pin,), descendants=True)
                    node.pin_read_errors.pop(pin, None)
            node.transformer_config = cfg
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
            futures = self._delete(self._prefix + (name,))
            if futures:
                from .attachments.fs.service import get_service

                timeout = get_service().delivery_timeout
                for future in futures:
                    future.result(timeout)

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
        from seamless_transformer.compiled_transformer import (
            CompiledTransformer,
            DirectCompiledTransformer,
        )
        from seamless_transformer.transformer_class import (
            BashTransformer,
            DirectBashTransformer,
            DirectPythonTransformer,
            PythonBashBaseTransformer,
            PythonTransformer,
        )

        node = self._graph.nodes[path]
        cfg = node.transformer_config
        is_direct = cfg.call_mode == "direct"
        if cfg.compilation is not None:
            cls = DirectCompiledTransformer if is_direct else CompiledTransformer
        elif cfg.language == "python":
            cls = DirectPythonTransformer if is_direct else PythonTransformer
        elif cfg.language == "bash":
            cls = DirectBashTransformer if is_direct else BashTransformer
        else:
            cls = PythonBashBaseTransformer
        bound_cls = cls.__dict__.get("_workflow_bound_class")
        if bound_cls is None:
            base_cls = cls

            def bound_setattr(instance, name, value):
                if name == "result" and getattr(instance, "_workflow_backend", None) is not None:
                    from .errors import ReadOnlyEndpointError

                    raise ReadOnlyEndpointError("Transformer result is read-only")
                base_cls.__setattr__(instance, name, value)

            def bound_call(instance, *args, **kwargs):
                backend = getattr(instance, "_workflow_backend", None)
                if backend is not None and is_direct and not args and not kwargs:
                    return backend.run()
                return base_cls.__call__(instance, *args, **kwargs)

            def bound_mount(instance):
                from .attachments.api import MountHandle

                return MountHandle(instance._workflow_backend)

            bound_cls = type(
                cls.__name__,
                (cls,),
                {
                    "__setattr__": bound_setattr,
                    "__call__": bound_call,
                    "mount": property(bound_mount),
                },
            )
            cls._workflow_bound_class = bound_cls
        handle = bound_cls.__new__(bound_cls)
        object.__setattr__(handle, "_workflow_backend", BoundTransformerBackend(self, path))
        return handle

    def _assign(self, path: NodePath, value: Any) -> None:
        from seamless_transformer.transformer_class import TransformerCore

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
                    source_type = self._source_celltype(value)
                    endpoint = self._endpoint(value)
                    if not endpoint.can_source or endpoint.node_path == path or self._would_cycle(endpoint.node_path, path):
                        raise DependencyError("Invalid source for transformer replacement")
                    self._check_deep_link(endpoint)
                    self._replace_current_checksum(path, None)
                    self._release_node_producers(node, path)
                    self._release_code_checksum(path)
                    records = list(self._runtime.superseded_runs.pop(path, ()))
                    current = self._runtime.current_runs.pop(path, None)
                    if current is not None: records.append(current)
                    for record in records:
                        if record.et is not None: self._effects.append(record.et.cancel)
                    self._replace_edges_to(path)
                    node.kind = "cell"
                    node.cell_config = CellConfig(celltype=source_type)
                    node.transformer_config = None
                    node.transformer_pin_producers = {}
                    node.exception = None
                    self._sync_module_refholds()
                    self._sync_superseded_refholds()
                    self._add_endpoint_edge(value, self._cell_endpoint(path))
                elif isinstance(value, (TransformerCore, PreparedTransformer)):
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
            # cells.md, *Assigning an anonymous handle*: the new node takes the
            # handle's celltype and is fed by an edge from it -- for an
            # anonymous handle, a dummy (identity) edge from its anonymous
            # cell.  Nothing is renamed, and the handle stays what it was.
            celltype = self._source_celltype(value)
            # An ill-formed deep link raises before the new node exists.
            self._check_deep_link(self._endpoint(value))
            self._create_cell(path, celltype=celltype)
            self._add_endpoint_edge(value, self._cell_endpoint(path))
        elif isinstance(value, (Cell, PreparedCell)):
            self._create_cell_from_builder(path, value)
        elif isinstance(value, (TransformerCore, PreparedTransformer)):
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
            return self._delete_subtree(path)
        if path in self._graph.namespaces or self._graph.has_prefix(path):
            return self._delete_subtree(path)
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
        cleanup_futures = []
        for node_path in self._graph.descendants(path):
            future = self._mount_detach(node_path, derive=False)
            if future is not None:
                cleanup_futures.append(future)
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
        return cleanup_futures

    def _create_cell(self, path, *, celltype="mixed"):
        if path in self._graph.nodes:
            raise NodeError(path)
        node = Node(kind="cell", cell_config=CellConfig(celltype=celltype))
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
        if node.mount and cell.celltype != node.cell_config.celltype:
            raise ValueError("Mounted celltype cannot change; unmount first")
        if node.mount is not None:
            self._mount_detach(path, delete=False, derive=False)
            node.mount = None
            node.mount_inactive = False
        session = self._mount_sessions.get(path)
        input_ref = cell._input_ref
        new_config = CellConfig(
            cell.celltype,
            cell.validator,
            cell.validator_language,
            bool(getattr(cell, "scratch", False)),
        )
        node.cell_root_expression = None
        if isinstance(input_ref, (Cell, PreparedCell)):
            if isinstance(input_ref, Cell) and input_ref._workflow_backend is not None:
                self._add_endpoint_edge(input_ref, self._cell_endpoint(path))
            else:
                upstream = self._graph.first_free("cell")
                self._create_cell_from_builder(upstream, input_ref)
                self._add_edge(upstream, path)
        elif self._is_bound_source(input_ref):
            self._add_endpoint_edge(input_ref, self._cell_endpoint(path))
        elif isinstance(input_ref, Checksum):
            # Acquire the replacement before mutating the graph's semantic
            # configuration or releasing the old producer.
            checksum = input_ref
            producer = self._retain_producer(
                checksum, cell.input_celltype or cell.celltype, scratch=new_config.scratch
            )
            old_producer = node.cell_root_producer
            node.cell_config = new_config
            node.cell_root_producer = producer
            self._revisions[path] = self._revisions.get(path, 0) + 1
            if old_producer is not None:
                self._release_producer(old_producer, path)
            self._remove_edges_targeting(path, (), descendants=True)
        elif isinstance(input_ref, Expression):
            from seamless.checksum.expression import parse_path

            links = []
            root = input_ref
            while isinstance(root, Expression):
                links.append(root)
                root = root._input_ref
            # A link that projects into a different result celltype becomes a
            # path link followed by a conversion link, which fuse back into
            # that one Expression (expressions.md, *Fusion*).
            if isinstance(root, Checksum):
                links.reverse()
                root_type = links[0].input_celltype
                source_path = self._graph.first_free("cell")
                self._create_cell(source_path, celltype=root_type)
                source_node = self._graph.nodes[source_path]
                source_node.cell_config.scratch = True
                source_node.cell_root_producer = self._retain_producer(
                    root, root_type, scratch=True,
                )
                local = []
                steps = []
                current_type = root_type
                for link in links:
                    components = [item for kind, item in parse_path(link.path)]
                    if components:
                        local.extend(components)
                        current_type = projected_celltype(current_type, tuple(components))
                    if link.celltype != current_type:
                        steps.append((len(local), link.celltype))
                        current_type = link.celltype
                node.cell_config = new_config
                old_producer = node.cell_root_producer
                node.cell_root_producer = None
                self._revisions[path] = self._revisions.get(path, 0) + 1
                if old_producer is not None:
                    self._release_producer(old_producer, path)
                self._remove_edges_targeting(path, (), descendants=True)
                self._add_edge(
                    source_path + tuple(local), path,
                    source_celltype=current_type,
                    source_conversion=bool(steps),
                    source_conversion_before=bool(steps and steps[0][0] == 0),
                    source_conversion_steps=tuple(steps),
                )
            else:
                node.cell_config = new_config
                node.cell_root_expression = input_ref
                old_producer = node.cell_root_producer
                node.cell_root_producer = None
                self._revisions[path] = self._revisions.get(path, 0) + 1
                if old_producer is not None:
                    self._release_producer(old_producer, path)
                self._remove_edges_targeting(path, (), descendants=True)
        elif input_ref is not None:
            raise TypeError(
                f"Cannot bind a Cell whose input_ref is {type(input_ref).__name__}"
            )
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

    def _transformer_config_from_frozen(self, frozen, *, direct=False):
        codebuf = frozen.codebuf
        code_checksum = (
            None
            if codebuf is None
            else codebuf
            if isinstance(codebuf, Checksum)
            else codebuf.get_checksum()
        )
        cfg = TransformerConfig(
            code=codebuf,
            code_checksum=code_checksum,
            language=frozen.language,
            callable=frozen.callable,
            pins=set(frozen.celltypes) - {"result"},
            celltypes=copy.deepcopy(frozen.celltypes),
            optional_pins=set(frozen.optional_pins),
            modules=copy.deepcopy(frozen.modules),
            globals=copy.deepcopy(frozen.globals),
            meta=copy.deepcopy(frozen.meta),
            environment=copy.deepcopy(frozen.environment),
            scratch=frozen.scratch,
            local=frozen.local,
            direct_print=frozen.direct_print,
            call_mode="direct" if direct else frozen.call_mode,
            schema=frozen.schema, compilation=copy.deepcopy(frozen.compilation),
            objects=copy.deepcopy(frozen.objects), header=frozen.header,
        )
        cfg.pins.update(frozen.args)
        cfg.celltypes.setdefault("result", "mixed")
        from .configuration import fingerprint
        fingerprint(cfg)
        return cfg, frozen

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
            from seamless_transformer.optional_pins import pin_signature
            signature = pin_signature(inspect.signature(code))
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
            cfg, frozen = transformer.config, transformer.frozen
            self._graph.nodes[path] = Node(kind="transformer", transformer_config=cfg)
            self._retain_code_checksum(path, cfg.code_checksum)
            for pin, value in frozen.args.items():
                self._set_transformer_pin(path, pin, value,
                    input_celltype=frozen.input_celltypes.get(pin))

        except Exception:
            self._delete_subtree(path)
            raise

    def _replace_transformer_from_builder(self, path, transformer):
        node = self._graph.nodes[path]
        cfg, frozen = transformer.config, transformer.frozen
        if cfg.signature_parameters() is None:
            cfg.pins.update(node.transformer_config.pins)
        removed_pins = node.transformer_config.pins - cfg.pins
        old_producers = node.transformer_pin_producers
        old_code_refholds = self._code_refholds.copy()
        old_module_refholds = self._module_refholds.copy()
        # Validate every replacement edge before acquiring/publishing configuration.
        for pin, value in frozen.args.items():
            ep = self._endpoint(value)
            if ep is not None:
                source = self._source_path(value)
                source_node, _ = self._graph.resolve_existing(source)
                if source_node == path or self._would_cycle(source_node, path):
                    raise DependencyError("Dependency cycle")
                self._check_authority(path, (pin,))

        new_producers = {
            pin: producer for pin, producer in old_producers.items()
            if pin in cfg.pins and pin not in frozen.args
        }
        staged_checksums = []
        published = False
        try:
            # Acquire all replacement state before touching the old semantic
            # fields.  This makes a failed conversion leave the old node live.
            for pin, value in frozen.args.items():
                if self._endpoint(value) is not None:
                    continue
                checksum = checksum_for_value(value, cfg.celltypes.get(pin, "mixed"))
                new_producers[pin] = self._retain_producer(
                    checksum, frozen.input_celltypes.get(pin) or cfg.celltypes.get(pin, "mixed"),
                    scratch=False,
                )
                staged_checksums.append(checksum)

            new_code_checksum = normalize_checksum(cfg.code_checksum)
            new_code_checksum.incref_refholder(scratch=False)
            staged_checksums.append(new_code_checksum)

            new_module_refs = {}
            for module_name, module in cfg.modules.items():
                if not isinstance(module, Checksum):
                    continue
                checksum = normalize_checksum(module)
                scratch = False
                checksum.incref_refholder(scratch=scratch)
                staged_checksums.append(checksum)
                new_module_refs[(path, module_name)] = (checksum, scratch)

            # Publish the fully acquired replacement as one semantic state.
            node.transformer_config = cfg
            node.transformer_pin_producers = new_producers
            self._code_refholds[path] = new_code_checksum
            self._module_refholds = {
                role: claim
                for role, claim in self._module_refholds.items()
                if role[0] != path
            }
            self._module_refholds.update(new_module_refs)
            published = True
            staged_checksums.clear()

            for pin, producer in old_producers.items():
                if new_producers.get(pin) is not producer:
                    self._release_producer(producer, path + (pin,))
            for pin in removed_pins | set(frozen.args):
                self._remove_edges_targeting(path, (pin,), descendants=True)
            self._remove_edges_targeting(path, ("code",), descendants=True)
            self._revisions[path] = self._revisions.get(path, 0) + 1
            old_code_checksum = old_code_refholds.get(path)
            if old_code_checksum is not None:
                old_code_checksum.decref_refholder()
            for role, (checksum, _scratch) in old_module_refholds.items():
                if role[0] == path:
                    checksum.decref_refholder()

            for pin, value in frozen.args.items():
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
        if session and checksum is None:
            raise AuthorityError("Cannot clear a mounted cell; unmount first")
        if session: session.sense_error = None
        if checksum is None:
            for lease in self._mount_node_leaves.pop(path, ()): lease._release_refholds()
        node = self._graph.nodes[path]
        producer = None if checksum is None else self._retain_producer(
            checksum, celltype, scratch=node.cell_config.scratch
        )
        old_producer = node.cell_root_producer
        node.cell_root_producer = producer
        self._revisions[path] = self._revisions.get(path, 0) + 1
        if old_producer is not None:
            self._release_producer(old_producer, path)
        if clear_edges:
            self._remove_edges_targeting(path, (), descendants=True)

    def _set_cell_value(self, path, local, value):
        self._cell_operation(path, local, value)

    def _set_cell_checksum(self, path, local, checksum, *, input_celltype=None):
        self._cell_operation(path, local, checksum, checksum_rhs=True, input_celltype=input_celltype)

    def _cell_operation(self, node_path, local, value, *, checksum_rhs=False, detach=False, input_celltype=None):
        node = self._graph.nodes[node_path]
        local = tuple(local)
        endpoint = self._endpoint(value)
        if endpoint is not None:
            self._add_endpoint_edge(value, self._cell_endpoint(node_path, local), detach=detach)
        else:
            self._validate_write(node_path, local, detach)
            if local:
                raise RuntimeError("Sub-path values must be prepared outside the controller")
            self._set_cell_root_with_edges(node_path, value, input_celltype or node.cell_config.celltype, clear_edges=detach)
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

    def _set_transformer_pin(self, node_path, pin, value, *, input_celltype=None, detach=True, checksum_rhs=False):
        if not isinstance(pin, str) or not pin:
            raise PathError("Transformer pin name must be non-empty")
        cfg = self._graph.nodes[node_path].transformer_config
        if not detach:
            self._check_authority(node_path, (pin,))
        cfg.check_pin_name(pin)
        self._graph.nodes[node_path].pin_read_errors.pop(pin, None)
        cfg.pins.add(pin)
        cfg.celltypes.setdefault(pin, "mixed")
        endpoint = self._endpoint(value)
        if endpoint is not None:
            self._add_endpoint_edge(value, self._transformer_endpoint(node_path, pin), detach=True)
        else:
            if checksum_rhs and value is None:
                self._clear_transformer_pin(node_path, pin)
                return
            checksum = checksum_for_value(value, cfg.celltypes.get(pin, "mixed"))
            producer = self._retain_producer(
                checksum, input_celltype or cfg.celltypes.get(pin, "mixed"),
                scratch=False,
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
        # Code is input-side configuration, so its Context claim publishes
        # it even when the transformer's result is scratch.
        checksum.incref_refholder(scratch=False)
        self._code_refholds[tuple(path)] = checksum

    def _replace_code_checksum(self, path, checksum):
        old = self._code_refholds.get(tuple(path))
        new = None if checksum is None else normalize_checksum(checksum)
        if new is not None:
            new.incref_refholder(scratch=False)
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
                    active[(path, module_name)] = (
                        normalize_checksum(module),
                        False,
                    )
        old_refholds = self._module_refholds
        acquired = []
        try:
            for role, (checksum, scratch) in active.items():
                old = old_refholds.get(role)
                old_checksum = old[0] if old is not None else None
                old_scratch = old[1] if old is not None else None
                if old_checksum is None or old_checksum != checksum or old_scratch != scratch:
                    checksum.incref_refholder(scratch=scratch)
                    acquired.append(checksum)
        except Exception:
            for checksum in reversed(acquired):
                checksum.decref_refholder()
            raise

        # Publish the new operational map only after every new role is held,
        # then release roles that are absent or replaced.  Overwriting the map
        # first loses the old checksum and strands its refholder reference.
        self._module_refholds = active
        for role, (checksum, scratch) in old_refholds.items():
            if active.get(role) != (checksum, scratch):
                checksum.decref_refholder()

    def _delete_transformer_pin(self, node_path, pin):
        cfg = self._graph.nodes[node_path].transformer_config
        if pin not in cfg.pins or cfg.signature_parameters() is not None:
            raise AttributeError(pin)
        cfg.pins.remove(pin)
        cfg.celltypes.pop(pin, None)
        cfg.optional_pins.discard(pin)
        self._clear_transformer_pin(node_path, pin)

    def _clear_transformer_pin(self, node_path, pin):
        node = self._graph.nodes[node_path]
        producer = node.transformer_pin_producers.pop(pin, None)
        if producer is not None:
            self._release_producer(producer, node_path + (pin,))
        self._remove_edges_targeting(node_path, (pin,), descendants=True)
        node.pin_states.pop(pin, None)
        self._derive_all()

    def _pin_snapshot(self, node_path, pin):
        if node_path not in self._graph.nodes:
            raise StaleWorkflowHandleError(f"Endpoint {node_path!r} is stale")
        node = self._graph.nodes[node_path]
        cfg = node.transformer_config
        if node.kind != 'transformer' or pin not in cfg.pins:
            raise AttributeError(pin)
        edge = self._incoming_edge(node_path, (pin,))
        producer = node.transformer_pin_producers.get(pin)
        if edge is not None:
            input_type = self._edge_input_celltype(edge)
            source = self._public_source(edge.source)
        else:
            input_type = producer.celltype if producer else None
            source = None
        state, checksum, error = node.pin_states.get(pin, ('unwired', None, None))
        return state, error, source, input_type, Lease(checksum, cfg.celltypes.get(pin, 'mixed'))

    def _record_pin_read_error(self, node_path, pin, identity, error):
        state, _, _, input_type, lease = self._pin_snapshot(node_path, pin)
        try:
            if state != 'complete' or lease.checksum is None:
                return
            current = (lease.checksum.hex(), input_type, lease.celltype)
            if current != identity:
                return
            self._graph.nodes[node_path].pin_read_errors[pin] = (identity, error)
        finally:
            lease._release_refholds()
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
        return BoundEndpoint(self.top_id, tuple(node_path), "transformer-code" if pin == "code" else "transformer-input", () if pin is None else (pin,), False, True, True)

    def _check_deep_link(self, source_ep, target=None):
        """Refuse a deep link outside the deep tables, before anything changes.

        deep-celltypes.md, *Bound wiring around the step*: writing an
        ill-formed deep link raises ``ValueError`` -- an integer key, a slice,
        a second step or an illegal deep conversion -- and the graph is left
        unchanged.  Only a graph loaded by `set_graph`, or a retype that
        invalidates existing wiring, derives the defect instead.

        ``target``, when it is the root of an existing cell, adds that cell's
        implicit conversion of a pathless source: ``ctx.x = ctx.d`` with ``d``
        deep converts ``d``'s celltype into ``x``'s, and that conversion is
        held to the deep table like any other.  A pathed source converting
        into its target is the wiring rule's ``TypeError`` instead.
        """
        if source_ep is None or source_ep.node_path not in self._graph.nodes:
            return
        local = tuple(source_ep.local_path)
        steps = tuple(source_ep.conversion_steps)
        if source_ep.conversion and not steps:
            position = 0 if source_ep.conversion_before else len(local)
            steps = ((position, source_ep.celltype),)
        root_type = self._node_celltype(source_ep.node_path)
        if (not local and target is not None and target.endpoint_kind == "cell-result"
                and not target.local_path and target.node_path in self._graph.nodes
                and self._graph.nodes[target.node_path].kind == "cell"):
            target_type = self._graph.nodes[target.node_path].cell_config.celltype
            if target_type != (source_ep.celltype or root_type):
                steps = steps + ((0, target_type),)
        error = _deep_recipe_error(root_type, local, steps)
        if error is not None:
            raise error

    def _add_endpoint_edge(self, source, target, *, detach=False):
        source_ep = self._endpoint(source)
        if source_ep is None:
            raise DependencyError("Source is not a bound workflow endpoint")
        if source_ep.top_id != self.top_id or target.top_id != self.top_id:
            raise DependencyError("Cross-top-level dependencies are not supported")
        if not source_ep.can_source:
            raise DependencyError("Endpoint cannot be used as a source")
        self._check_deep_link(source_ep, target)
        if target.endpoint_kind == "cell-subvalue":
            if len(target.local_path) != 1 or isinstance(target.local_path[0], slice):
                raise PathError("Cell connection targets are limited to one point component")
            if self._graph.nodes[target.node_path].cell_config.celltype not in PIN_CELLTYPES:
                raise PathError("Cell subvalue connections require a container-capable Cell")
            target_type = self._graph.nodes[target.node_path].cell_config.celltype
            if target_type in _DEEP_CELLTYPES:
                member_type = "mixed" if target_type == "deepcell" else "bytes"
                source_type = source_ep.celltype or self._node_celltype(source_ep.node_path)
                if not isinstance(target.local_path[0], str):
                    raise ValueError("Deep Cell connection targets require a string key")
                if source_type != member_type:
                    raise TypeError("Deep Cell connections require the member celltype")
            if source_ep.local_path:
                target_type = projected_celltype(target_type, target.local_path)
                source_type = source_ep.celltype or self._node_celltype(source_ep.node_path)
                if target_type != source_type and not source_ep.conversion:
                    raise _projected_source_type_error(
                        source_ep, source_type, target_type, target.node_path,
                        target_local=target.local_path,
                        root_type=self._node_celltype(source_ep.node_path),
                    )
        elif target.endpoint_kind == "transformer-input":
            if len(target.local_path) != 1:
                raise PathError("Transformer targets must address a whole pin")
            celltype = self._graph.nodes[target.node_path].transformer_config.celltypes.get(target.local_path[0], 'mixed')
            source_type = source_ep.celltype or self._node_celltype(source_ep.node_path)
            if source_ep.local_path and source_type != celltype:
                raise _projected_source_type_error(
                    source_ep, source_type, celltype, target.node_path,
                    target_pin=target.local_path[0],
                    root_type=self._node_celltype(source_ep.node_path),
                )
            if (not source_ep.local_path and not source_ep.conversion
                    and source_type != celltype):
                checksum = self._get_checksum(source_ep.node_path, ())
                if checksum is not None:
                    from seamless.checksum.null import is_null
                    if not is_null(checksum):
                        from seamless.checksum.expression import validate_expression_shape

                        validate_expression_shape("", source_type, celltype)
        elif target.endpoint_kind == "cell-result" and source_ep.local_path:
            target_node = self._graph.nodes[target.node_path]
            target_type = target_node.cell_config.celltype
            source_type = source_ep.celltype or self._node_celltype(source_ep.node_path)
            if target_type != source_type and not source_ep.conversion:
                raise _projected_source_type_error(
                    source_ep, source_type, target_type, target.node_path,
                    root_type=self._node_celltype(source_ep.node_path),
                )
        self._add_edge(
            source_ep.node_path + source_ep.local_path,
            target.node_path + target.local_path,
            detach=detach,
            source_celltype=source_ep.celltype,
            source_conversion=source_ep.conversion,
            source_conversion_before=source_ep.conversion_before,
            source_conversion_steps=source_ep.conversion_steps,
        )
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

    def _add_edge(self, source, target, *, detach=False, source_celltype=None,
                  source_conversion=False, source_conversion_before=False,
                  source_conversion_steps=()):
        source_node, _ = self._graph.resolve_existing(tuple(source))
        target_node, target_local = self._graph.resolve_existing(tuple(target))
        target_config = self._graph.nodes[target_node].cell_config
        if target_config is not None:
            incoming = self._incoming_for(target_node)
            if target_local and () in incoming:
                raise DependencyError("A root connection cannot be combined with sub-path connections")
            if not target_local and any(local for local in incoming):
                raise DependencyError("A root connection cannot be combined with sub-path connections")
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
        edge = Edge(
            tuple(source), tuple(target), source_celltype,
            source_conversion, source_conversion_before, tuple(source_conversion_steps),
            deep_member=bool(target_local and target_config is not None
                             and target_config.celltype in _DEEP_CELLTYPES),
        )
        # cells.md, *Symbols*: the edge creates its anonymous links, so their
        # symbols are assigned now, in arrival order.
        self._graph.edges.append(self._graph.register_edge_symbols(edge))
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
        """The incoming edge at ``local``, as its target derives and reports it.

        A dummy edge from a single link is looked through (``ContextGraph.own_link``).
        """
        for edge in self._graph.edges:
            try:
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                continue
            if target_node == node_path and target_local == tuple(local):
                return self._graph.own_link(edge)
        return None

    def _incoming_for(self, node_path):
        """The incoming edges by target path, as their target derives and reports them.

        cells.md, *The input*: "A dummy edge from a single link is looked
        through", so such an edge is the link itself, as the target's own
        incoming edge -- the form ``get_graph()`` saves and ``set_graph()``
        loads.  State derivation, ``.source`` and ``input_celltype`` all read
        edges through here, so a running Context and a reloaded one agree.
        """
        result = {}
        for edge in self._graph.edges:
            try:
                target_node, target_local = self._graph.resolve_existing(edge.target)
            except KeyError:
                continue
            if target_node == node_path:
                result[target_local] = self._graph.own_link(edge)
        return result

    def _value_from_producer(self, producer):
        return producer.checksum

    def _retain_producer(self, checksum, celltype, *, scratch=False):
        checksum = normalize_checksum(checksum)
        from seamless.checksum.null import canonicalize_checksum
        checksum = canonicalize_checksum(checksum, celltype)
        checksum.incref_refholder(scratch=scratch)
        return ConstantProducer(checksum, celltype)

    def _release_producer(self, producer, owner):
        checksum = producer.checksum
        checksum.decref_refholder()

    def _replace_current_checksum(self, node_path, checksum):
        """Acquire a node current result before replacing its semantic field."""

        node = self._graph.nodes[node_path]
        new_checksum = None if checksum is None else normalize_checksum(checksum)
        if new_checksum is not None:
            from seamless.checksum.null import canonicalize_checksum
            new_checksum = canonicalize_checksum(
                new_checksum, self._node_celltype(node_path)
            )
        old_checksum = node.current_checksum
        if new_checksum is not None:
            # The node owns its result, so its scratch policy decides.
            new_checksum.incref_refholder(scratch=self._node_scratch(node))
        node.current_checksum = new_checksum
        if old_checksum is not None:
            old_checksum.decref_refholder()

    def _refheld_checksums(self):
        if self._refholds_released:
            return ()
        claims = []
        seen_root_expressions = set()
        for path, node in self._graph.nodes.items():
            if node.cell_root_producer is not None:
                claims.append((node.cell_root_producer.checksum, f"cell:{':'.join(path)}:literal"))
            if node.cell_root_expression is not None:
                expression = node.cell_root_expression
                if id(expression) not in seen_root_expressions:
                    seen_root_expressions.add(id(expression))
                    for checksum, role in expression._refheld_checksums():
                        claims.append(
                            (checksum, f"cell:{':'.join(path)}:expression:{role}")
                        )
            for pin, producer in node.transformer_pin_producers.items():
                claims.append((producer.checksum, f"transformer:{':'.join(path)}:pin:{pin}"))
            if node.kind == "transformer":
                cfg = node.transformer_config
                code_checksum = cfg.code_checksum
                if code_checksum is not None and self._incoming_edge(path, ("code",)) is None:
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
        for symbol, (checksum, _scratch) in self._anonymous_current_refholds.items():
            claims.append((checksum, f"anonymous:{symbol}:current"))
        for (path, pin), (checksum, _scratch) in self._edge_pin_refholds.items():
            role = "code" if pin == "code" else f"pin:{pin}"
            claims.append((checksum, f"transformer:{':'.join(path)}:{role}"))
        return claims

    def _release_refholds(self):
        if self._refholds_released:
            return
        for checksum, _role in list(self._refheld_checksums()):
            checksum.decref_refholder()
        self._superseded_refholds.clear()
        self._anonymous_current_refholds.clear()
        self._edge_pin_refholds.clear()
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
        self._anonymous_current_updates = {}
        self._edge_code_states = {}
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
        self._sync_edge_pin_refholds()
        self._sync_anonymous_current_refholds()
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
        for path, records in self._runtime.superseded_runs.items():
            for record in records:
                if record.phase != "cancelled" and record.result_checksum is not None:
                    active[id(record)] = (
                        record.result_checksum, self._node_scratch(self._graph.nodes[path])
                    )
        for record_id, (checksum, _scratch) in list(self._superseded_refholds.items()):
            if record_id not in active:
                checksum.decref_refholder()
                del self._superseded_refholds[record_id]
        for record_id, (checksum, scratch) in active.items():
            if record_id not in self._superseded_refholds:
                checksum.incref_refholder(scratch=scratch)
                self._superseded_refholds[record_id] = (checksum, scratch)

    def _sync_edge_pin_refholds(self):
        wanted = {}
        for path, node in self._graph.nodes.items():
            if node.kind != "transformer":
                continue
            for pin, (state, checksum, _error) in node.pin_states.items():
                if state == "complete" and checksum is not None and self._incoming_edge(path, (pin,)) is not None:
                    wanted[(path, pin)] = (
                        checksum, self._edge_target_scratch(node, (pin,)))
            code = self._edge_code_states.get(path)
            if code is not None:
                wanted[(path, "code")] = (
                    code, self._edge_target_scratch(node, ("code",)))
        old = self._edge_pin_refholds
        acquired = []
        try:
            for key, (checksum, scratch) in wanted.items():
                if old.get(key) != (checksum, scratch):
                    checksum.incref_refholder(scratch=scratch)
                    acquired.append(checksum)
        except Exception:
            for checksum in reversed(acquired):
                checksum.decref_refholder()
            raise
        for key, (checksum, scratch) in old.items():
            if wanted.get(key) != (checksum, scratch):
                checksum.decref_refholder()
        self._edge_pin_refholds = wanted

    def _anonymous_symbol(self, edge, recipe, index=0):
        """The symbol of ``edge``'s anonymous link at ``index``, innermost first.

        It was assigned when the edge was added (``register_edge_symbols``), so
        this is a lookup.  The stored recipe is used rather than ``recipe``,
        which is recomputed from the current celltypes: a symbol and its
        entry's celltype are fixed when the anonymous cell is created.
        """
        if len(edge.source_chain) > index:
            recipe = edge.source_chain[index]
        return self._graph.assign_symbol(recipe)

    def _evaluate_deep_step(self, edge, source_node, checksum, root_type, local, deep, *, scratch,
                            materialize=False, input_materializer=None):
        """Evaluate the deep step of ``edge``'s source recipe; return the child.

        ``deep`` is ``split_deep_step``'s answer.  The conversions before the
        step and the step itself are separate Expressions (a deep step forms no
        pair, expressions.md, *Fusion*), so their anonymous cells are not
        elided: each one's result checksum is produced and held, unless the
        step is the last link of the recipe (cells.md, *Anonymous cells,
        symbols and elision*).
        """
        prefix, deep_type, member, rest_local, rest_steps = deep
        current = root_type
        for index, converted in enumerate(prefix):
            if converted != current:
                state, checksum, error = self._projection(
                    checksum, (), current, converted, scratch=True,
                    materialize=False,
                    input_materializer=input_materializer if index == 0 else None)
                if state != "complete":
                    return state, None, error
            symbol = self._anonymous_symbol(edge, (source_node, converted, ()), index=index)
            self._anonymous_current_updates[symbol] = (normalize_checksum(checksum), True)
            current = converted
        step = tuple(local[:1])
        state, child, error = self._projection(
            checksum, step, deep_type, member,
            scratch=scratch if not (rest_local or rest_steps) else True,
            materialize=materialize if not (rest_local or rest_steps) else False,
            input_materializer=input_materializer if not prefix else None)
        if state != "complete":
            return state, None, error
        if rest_local or rest_steps:
            symbol = self._anonymous_symbol(edge, (source_node, member, step), index=len(prefix))
            self._anonymous_current_updates[symbol] = (normalize_checksum(child), True)
        return "complete", child, None

    def _sync_anonymous_current_refholds(self):
        active = self._anonymous_current_updates
        old = self._anonymous_current_refholds
        updated = {}
        acquired = []
        try:
            for symbol, (checksum, scratch) in active.items():
                claim = (checksum, scratch)
                if old.get(symbol) == claim:
                    updated[symbol] = old[symbol]
                else:
                    checksum.incref_refholder(scratch=scratch)
                    acquired.append(checksum)
                    updated[symbol] = claim
        except Exception:
            for checksum in reversed(acquired):
                checksum.decref_refholder()
            raise
        for symbol, (checksum, scratch) in old.items():
            if updated.get(symbol) != (checksum, scratch):
                checksum.decref_refholder()
        self._anonymous_current_refholds = updated

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
        producer = node.cell_root_producer
        if () in incoming or not incoming:
            edge = incoming.get(())
            if edge is not None:
                source_path, source_local = self._graph.resolve_existing(edge.source)
                source_node = self._graph.nodes[source_path]
                if (source_local and source_node.state == "complete"
                        and self._graph.edge_miswiring(edge) is None
                        and not edge.source_conversion_before and not edge.source_conversion):
                    # One step below a deep parent, the source is the member
                    # celltype (deep-celltypes.md, *Handles and writes one step
                    # below a deep parent*); a deep link outside the deep tables
                    # is statically ill-formed (cells.md, *Connecting*).
                    if (_deep_recipe_error(self._node_celltype(source_path), source_local) is not None
                            or (cfg.celltype not in {"deepcell", "deepfolder", "folder"}
                                and self._celltype_for_path(source_path, source_local) != cfg.celltype
                                and not edge.source_conversion)):
                        self._replace_current_checksum(path, None)
                        node.state, node.block_reason, node.exception = "miswired", None, None
                        return
                    source_checksum = source_node.current_checksum
                    projection_path = source_local
                    input_type = self._node_celltype(source_path)
                    fused = self._fused_projection_source(source_path, source_local)
                    if fused is not None:
                        source_checksum, input_type, projection_path = fused
                    input_materializer = (
                        self._scratch_source_materializer(source_path)
                        if source_checksum == source_node.current_checksum else None
                    )
                    member = _deep_barrier(input_type, projection_path, cfg.celltype)
                    if member is not None:
                        # The deep step is a fusion barrier, so its anonymous cell
                        # is evaluated and held, not elided (cells.md, *Anonymous
                        # cells, symbols and elision*); `_projection` below reuses
                        # this evaluation for the step.
                        step = tuple(projection_path[:1])
                        child_state, child, _ = self._projection(
                            source_checksum, step, input_type, member, scratch=True,
                            input_materializer=input_materializer,
                        )
                        if child_state == "complete" and len(projection_path) > 1:
                            symbol = self._anonymous_symbol(edge, (source_path, member, step))
                            self._anonymous_current_updates[symbol] = (normalize_checksum(child), True)
                    state, checksum, error = self._projection(
                        source_checksum, projection_path,
                        input_type, cfg.celltype,
                        cfg.validator, cfg.validator_language, scratch=cfg.scratch,
                        input_materializer=input_materializer,
                    )
                    self._replace_current_checksum(path, checksum)
                    node.state, node.block_reason, node.exception = state, None, error
                    return
                state, checksum = self._source_state(edge)
                if state != "complete":
                    error = self._edge_errors.pop(edge.target, None)
                    if state == "failed" and error is not None:
                        self._replace_current_checksum(path, None)
                        node.state, node.block_reason, node.exception = "failed", None, error
                        return
                    if state == "miswired":
                        self._replace_current_checksum(path, None)
                        node.state, node.block_reason, node.exception = "miswired", None, None
                    else:
                        self._apply_upstream_state(node, (state, checksum))
                    return
                if (not edge.source_conversion and not source_local
                        and self._node_celltype(source_path) != cfg.celltype):
                    state, checksum, error = self._projection(
                        checksum, (), self._node_celltype(source_path), cfg.celltype,
                        cfg.validator, cfg.validator_language, scratch=cfg.scratch,
                        input_materializer=self._scratch_source_materializer(source_path),
                    )
                    self._replace_current_checksum(path, checksum)
                    node.state, node.block_reason, node.exception = state, None, error
                    return
                self._replace_current_checksum(path, checksum)
                node.state, node.block_reason, node.exception = "complete", None, None
                return
            elif node.cell_root_expression is not None:
                expression = node.cell_root_expression
                try:
                    checksum = expression.compute(execution=self._expression_execution)
                except Exception as exc:
                    self._replace_current_checksum(path, None)
                    node.state, node.block_reason = "failed", None
                    node.exception = exc
                    return
                self._replace_current_checksum(path, checksum)
                node.state, node.block_reason, node.exception = "complete", None, None
                return
            else:
                checksum = producer.checksum if producer else None
            error = None
            state = "complete" if checksum is not None else "unwired"
            input_type = self._effective_input_celltype(path)
            if checksum is not None and (input_type != cfg.celltype or cfg.validator is not None):
                state, checksum, error = self._projection(checksum, (), input_type, cfg.celltype, cfg.validator, cfg.validator_language,
                                                          scratch=cfg.scratch)
            self._replace_current_checksum(path, checksum)
            node.state, node.block_reason, node.exception = state, None, error
            return
        inputs = []
        pending = []
        for local, edge in incoming.items():
            state, checksum = self._source_state(edge)
            if state != "complete":
                pending.append(edge)
                continue
            source, _ = self._graph.resolve_existing(edge.source)
            source_type = edge.source_celltype or self._node_celltype(source)
            _, source_local = self._graph.resolve_existing(edge.source)
            if (cfg.celltype not in _DEEP_CELLTYPES and not source_local
                    and not edge.source_conversion and source_type != cfg.celltype):
                state, checksum, error = self._projection(
                    checksum, (), source_type, cfg.celltype, scratch=cfg.scratch
                )
                if state != "complete":
                    if state == "failed" and error is not None:
                        self._replace_current_checksum(path, None)
                        node.state, node.block_reason, node.exception = "failed", None, error
                        return
                    pending.append(edge)
                    continue
                source_type = cfg.celltype
            inputs.append((local, checksum, source_type))
        if pending:
            self._apply_pending(node, pending, join=True)
            return
        root = producer.checksum if producer else None
        root_type = producer.celltype if producer else cfg.celltype
        key = ("merge", root.hex() if root is not None else None, root_type,
               tuple((local, cs.hex(), ct) for local, cs, ct in inputs), cfg.celltype)
        state, checksum, error = self._demand(key, evaluate_cell, (root, root_type, tuple(inputs), cfg.celltype),
                                            [cs for _, cs, _ in inputs] + ([root] if root is not None else []))
        self._replace_current_checksum(path, checksum)
        node.state, node.block_reason, node.exception = state, None, error

    def _source_celltype(self, value):
        endpoint = self._endpoint(value)
        if endpoint.top_id != self.top_id:
            raise DependencyError("Cannot connect sources from a different top-level Context")
        return endpoint.celltype or self._node_celltype(endpoint.node_path)

    def _effective_input_celltype(self, path):
        return self._input_celltype_for_path(path, ())

    def _input_celltype_for_path(self, path, local):
        node = self._graph.nodes[path]
        incoming = self._incoming_for(path)
        for length in range(len(local), -1, -1):
            edge = incoming.get(tuple(local[:length]))
            if edge is not None:
                return self._edge_input_celltype(edge)
        producer = node.cell_root_producer
        return producer.celltype if producer is not None else None

    def _edge_input_celltype(self, edge):
        """The celltype an incoming edge delivers: its source's, at the source path.

        cells.md, *The input*: ``input_celltype`` is the celltype of whatever
        ``.source`` resolves to, one lookup with it.  ``edge`` comes from
        ``_incoming_for`` / ``_incoming_edge``, so a looked-through dummy edge
        is already the link itself: it reports the link's own source, at that
        source's celltype, exactly as after a save and reload.  An edge from
        an anonymous cell reports that cell's recorded celltype.
        """
        source, source_local = self._graph.resolve_existing(edge.source)
        if (source_local or edge.source_conversion) and edge.source_celltype is not None:
            return edge.source_celltype
        if source_local:
            return self._celltype_for_path(source, source_local)
        return self._node_celltype(source)

    def _public_cell_source(self, node_path, local):
        incoming = self._incoming_for(node_path)
        for length in range(len(local), -1, -1):
            edge = incoming.get(tuple(local[:length]))
            if edge is not None:
                source_path, source_local = self._graph.resolve_existing(edge.source)
                source_node = self._graph.nodes[source_path]
                backend = BoundCellBackend(
                    self, source_path, source_local,
                    readonly=source_node.kind != "cell",
                    projected_celltype=edge.source_celltype,
                    conversion=edge.source_conversion,
                    conversion_before=edge.source_conversion_before,
                    conversion_steps=edge.source_conversion_steps,
                )
                return Cell._from_backend(backend)
        return None

    def _node_celltype(self, path):
        node = self._graph.nodes[path]
        return node.cell_config.celltype if node.kind == "cell" else node.transformer_config.celltypes.get("result", "mixed")

    def _celltype_for_path(self, path, local=()):
        """Return the effective celltype of a node's projected handle.

        A transformer node is projected through its result, which may be deep
        like any cell (deep-celltypes.md, *The output side*).
        """
        return projected_celltype(self._node_celltype(path), tuple(local))

    def _projected_path_celltype(self, celltype, component):
        if isinstance(component, slice):
            return celltype
        if celltype == "deepcell":
            return "mixed"
        if celltype in {"deepfolder", "folder"}:
            return "bytes"
        return celltype

    def _node_scratch(self, node):
        if node.kind == "cell":
            return bool(node.cell_config.scratch)
        if node.kind == "transformer":
            return bool(node.transformer_config.scratch)
        return False

    def _edge_target_scratch(self, target_node, target_local):
        if target_node.kind == "cell":
            return bool(target_node.cell_config.scratch)
        if target_node.kind == "transformer":
            return bool(target_node.transformer_config.meta.get("allow_input_fingertip"))
        return False

    def _feeds_non_scratch_holder(self, path):
        # A holder overrules a scratch producer only while its result is the
        # holder's own buffer. Paths and buffer-producing conversions end that
        # checksum-preserving walk.
        from seamless.checksum.conversion import conversion_trivial, conversion_reinterpret

        preserving = conversion_trivial | conversion_reinterpret
        visited = {path}
        stack = [path]
        while stack:
            current = stack.pop()
            for edge in self._graph.edges:
                try:
                    source, source_local = self._graph.resolve_existing(edge.source)
                    target, local = self._graph.resolve_existing(edge.target)
                except KeyError:
                    continue
                if source != current:
                    continue
                if source_local:
                    continue
                source_type = self._node_celltype(source)
                steps = edge.source_conversion_steps
                if edge.source_conversion and not steps:
                    steps = ((0, edge.source_celltype or source_type),)
                if any((left, right) not in preserving and left != right
                       for left, right in zip(
                           (source_type,) + tuple(step[1] for step in steps[:-1]),
                           tuple(step[1] for step in steps),
                       )):
                    continue
                output_type = steps[-1][1] if steps else source_type
                node = self._graph.nodes[target]
                if node.kind == "transformer":
                    pin_type = node.transformer_config.celltypes.get(local[0], "mixed")
                    if output_type != pin_type and (output_type, pin_type) not in preserving:
                        continue
                    if not self._edge_target_scratch(node, local):
                        return True
                elif node.kind == "cell":
                    if local:
                        continue
                    cell_type = node.cell_config.celltype
                    if output_type != cell_type and (output_type, cell_type) not in preserving:
                        continue
                    if not node.cell_config.scratch:
                        return True
                    if target not in visited:
                        visited.add(target)
                        stack.append(target)
        return False

    def _projection(self, checksum, local, celltype, target_type=None, validator=None, validator_language=None,
                    scratch=True, materialize=False, input_expression=None,
                    input_materializer=None):
        """Evaluate one Expression under its edge target's scratch policy.

        ``materialize`` asks for reachable bytes when the target is a
        non-scratch transformer pin. A cell edge asks for a checksum.

        A path that crosses a deep step is two Expressions: the step yields the
        child checksum, and the rest is evaluated over that child
        (expressions.md, *Fusion*: a deep step is a barrier and forms no pair).
        """
        target_type = target_type or celltype
        local = tuple(local)
        member = _deep_barrier(celltype, local, target_type)
        if member is not None:
            state, child, error = self._projection(checksum, local[:1], celltype, member,
                                                   scratch=True, materialize=False,
                                                   input_expression=input_expression,
                                                   input_materializer=input_materializer)
            if state != "complete":
                return state, child, error
            try:
                first = Expression(
                    input_expression or checksum, path=_path_string(local[:1]),
                    input_celltype=celltype, celltype=member,
                )
            except (TypeError, ValueError) as exc:
                from .errors import execution_error

                return "failed", None, execution_error(exc)
            return self._projection(
                child, local[1:], member, target_type, validator, validator_language,
                scratch=scratch, materialize=materialize, input_expression=first,
            )
        try:
            Expression(
                checksum,
                path=_path_string(tuple(local)),
                input_celltype=celltype,
                celltype=target_type,
                validator=validator,
                validator_language=validator_language,
            )
        except (TypeError, ValueError) as exc:
            from .errors import execution_error

            return "failed", None, execution_error(exc)
        # Python slices are represented as strings in the content key.
        key = ("expression", checksum.hex(), _path_string(local), celltype, target_type,
               validator.hex() if isinstance(validator, Checksum) else validator, validator_language,
               bool(scratch), bool(materialize))
        state, result, error = self._demand(key, evaluate_projection,
            (checksum, tuple(local), celltype, target_type, validator,
             validator_language, bool(scratch), bool(materialize), input_expression,
             input_materializer),
            [checksum])
        return state, result, error

    def _path_expression(self, source, input_type, path, celltype):
        """Build ``path`` over ``source`` as an Expression, never fusing across a deep step.

        When ``path`` crosses a deep step, the step is its own Expression and
        the rest is a separate Expression over the child checksum
        (expressions.md, *Fusion*).
        """
        path = tuple(path)
        member = _deep_barrier(input_type, path, celltype)
        if member is not None:
            source = Expression(
                source, path=_path_string(path[:1]), input_celltype=input_type, celltype=member,
            )
            input_type, path = member, path[1:]
            if not path and celltype == member:
                return source
        return Expression(source, path=_path_string(path), input_celltype=input_type, celltype=celltype)

    def _demand(self, key, function, args, checksums):
        self._used_facts.add(key)
        fact = self._facts.get(key)
        if fact is not None:
            lease, error = fact
            return (
                ("failed" if error else "complete"),
                lease.checksum if lease else None,
                error,
            )
        if key not in self._jobs:
            leases = tuple(Lease(cs) for cs in checksums)
            self._jobs[key] = None
            import weakref

            owner = weakref.ref(self)
            execution = self._expression_execution

            async def work(leases=leases):
                try:
                    if function is evaluate_projection:
                        from seamless.checksum.expression import (
                            evaluate_expression_placed,
                            softcancel_expression,
                        )

                        cs, local, ct, target, validator, validator_language, scratch, materialize, input_expression, input_materializer = args
                        materialize_input = input_materializer
                        if input_expression is not None:
                            async def materialize_input():
                                return await input_expression._evaluate_internal_async(
                                    execution="local", scratch=True, materialize=True,
                                )
                        try:
                            checksum = await evaluate_expression_placed(
                                cs,
                                _path_string(local),
                                ct,
                                target,
                                validator=validator,
                                validator_language=validator_language,
                                execution=execution,
                                member_id=key,
                                scratch=scratch,
                                materialize=materialize,
                                materialize_input=materialize_input,
                            )
                            if checksum is None:
                                raise KeyError(_path_string(local))
                        except asyncio.CancelledError:
                            softcancel_expression(
                                (cs.hex(), _path_string(local), ct, target), key
                            )
                            raise
                    else:
                        worker_leases = tuple(Lease(lease.checksum) for lease in leases)

                        def evaluate(worker_leases=worker_leases):
                            try:
                                return function(*args)
                            finally:
                                for lease in worker_leases:
                                    lease._release_refholds()

                        checksum = await asyncio.to_thread(evaluate)
                    payload, error = Lease(checksum), None
                except Exception as exc:
                    from .errors import execution_error

                    payload, error = None, execution_error(exc)
                finally:
                    for lease in leases:
                        lease._release_refholds()
                context = owner()
                if context is not None and context._controller.accepting:
                    try:
                        context._controller.submit(
                            "_accept_fact", (key, payload, error), klass=5
                        )
                    except Exception:
                        if payload is not None:
                            payload._release_refholds()
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

    def _scratch_source_materializer(self, source_node):
        """Return local recomputation for a scratch named source, if it has one."""
        node = self._graph.nodes[source_node]
        if node.kind == "cell":
            incoming = self._incoming_for(source_node)
            if not node.cell_config.scratch or not incoming:
                return None
            from seamless.cell_class import _UNSET

            expression = self._build_cell_expression(source_node, (), _UNSET)
            upstream_materializer = None
            if set(incoming) == {()}:
                upstream_path, _ = self._graph.resolve_existing(incoming[()].source)
                if upstream_path != source_node:
                    upstream_materializer = self._scratch_source_materializer(upstream_path)

            async def materialize_cell():
                if upstream_materializer is not None:
                    await upstream_materializer()
                return await expression._evaluate_internal_async(
                    execution="local", scratch=True, materialize=True,
                )

            return materialize_cell
        if node.kind == "transformer" and node.transformer_config.scratch:
            from dataclasses import replace

            frozen = replace(
                self._freeze_transformer(source_node),
                local=True, scratch=True, signature=None,
            )

            async def materialize_transformer():
                from seamless_transformer.transformer_class import PythonBashBaseTransformer

                try:
                    builder = PythonBashBaseTransformer.__new__(PythonBashBaseTransformer)
                    transformation = builder._build_from_frozen(frozen)
                    result = await transformation.computation(require_value=True)
                    if result is None:
                        raise RuntimeError(transformation.exception or "Transformation failed")
                    return result
                finally:
                    for lease in frozen.leases:
                        lease._release_refholds()

            return materialize_transformer
        return None

    def _source_state(self, edge):
        self._edge_errors.pop(edge.target, None)
        # cells.md, *The handle and the node*: an anonymous cell whose recorded
        # celltype its source no longer projects to is `miswired`, and what it
        # feeds is blocked; a looked-through single link (``_incoming_for``)
        # is the target's own, so its defect is too.  The ordinary
        # target-side comparisons below apply to it as to any own edge.
        miswiring = self._graph.edge_miswiring(edge)
        if miswiring is not None:
            return miswiring, None
        source_node, source_local = self._graph.resolve_existing(edge.source)
        node = self._graph.nodes[source_node]
        target_path, target_local = self._graph.resolve_existing(edge.target)
        target_node = self._graph.nodes[target_path]
        edge_scratch = self._edge_target_scratch(target_node, target_local)
        materialize = target_node.kind == "transformer" and not edge_scratch
        root_type = self._node_celltype(source_node)
        if source_local:
            source_type = edge.source_celltype or self._celltype_for_path(
                source_node, source_local
            )
        elif edge.source_conversion:
            source_type = edge.source_celltype or root_type
        else:
            source_type = root_type
        target_type = root_type
        if (source_local and not edge.source_conversion
                and source_type != self._celltype_for_path(source_node, source_local)):
            return "miswired", None
        if target_node.kind == "cell":
            target_type = target_node.cell_config.celltype
            if edge.deep_member and target_type not in _DEEP_CELLTYPES:
                return "miswired", None
            if target_local:
                if target_type not in PIN_CELLTYPES:
                    return "miswired", None
                if target_type in _DEEP_CELLTYPES:
                    member_type = "mixed" if target_type == "deepcell" else "bytes"
                    if (len(target_local) != 1 or not isinstance(target_local[0], str)
                            or source_type != member_type):
                        return "miswired", None
        elif target_node.kind == "transformer" and target_local != ("code",):
            target_type = target_node.transformer_config.celltypes.get(target_local[0], "mixed")
        if source_local and target_node.kind == "cell" and not target_local:
            if (target_node.cell_config.celltype not in {"deepcell", "deepfolder", "folder"}
                    and source_type != target_node.cell_config.celltype
                    and not edge.source_conversion):
                return "miswired", None
        if source_local and target_node.kind == 'transformer' and target_local != ('code',):
            pin_type = target_node.transformer_config.celltypes.get(target_local[0], 'mixed')
            if source_type != pin_type and not edge.source_conversion:
                return 'miswired', None
        if (not source_local and not edge.source_conversion
                and target_node.kind == "transformer"
                and target_local != ("code",) and source_type != target_type):
            from seamless.checksum.expression import validate_expression_shape

            try:
                validate_expression_shape("", source_type, target_type)
            except (TypeError, ValueError):
                return "miswired", None
        if (not source_local and target_node.kind == "cell" and not target_local
                and source_type != target_type
                and (source_type in _DEEP_CELLTYPES or target_type in _DEEP_CELLTYPES)):
            # A cell's implicit conversion of a pathless deep link is held to
            # the deep table (deep-celltypes.md, *Bound wiring around the
            # step*); outside it the link is statically ill-formed.
            from seamless.checksum.expression import validate_expression_shape

            try:
                validate_expression_shape("", source_type, target_type)
            except (TypeError, ValueError):
                return "miswired", None
        conversion_steps = edge.source_conversion_steps
        if edge.source_conversion and not conversion_steps:
            position = 0 if edge.source_conversion_before else len(source_local)
            conversion_steps = ((position, source_type),)
        if _deep_recipe_error(root_type, source_local, conversion_steps) is not None:
            return "miswired", None
        if node.state == 'miswired' or node.block_reason == 'blocked-by-miswiring':
            return 'blocked-by-miswiring', None
        if node.state != "complete":
            if node.state == "blocked" and isinstance(node.block_reason, dict):
                reasons = set(node.block_reason.values())
                if "blocked-by-miswiring" in reasons:
                    return "blocked-by-miswiring", None
                if "blocked-by-unwired" in reasons:
                    return "blocked", None
                if "blocked-by-error" in reasons:
                    return "failed", None
            return ("failed" if node.block_reason == "blocked-by-error" else node.state), None
        root_checksum = node.current_checksum
        root_materializer = self._scratch_source_materializer(source_node)
        # Index into the edge's anonymous links of the first link evaluated below.
        chain_offset = 0
        deep = split_deep_step(root_type, source_local, conversion_steps)
        if deep is not None:
            # deep-celltypes.md, *Paths*: the step yields the child checksum, and
            # the rest of the recipe is ordinary wiring over that child.
            if root_checksum is None:
                return "unwired", None
            state, root_checksum, error = self._evaluate_deep_step(
                edge, source_node, root_checksum, root_type, source_local, deep,
                scratch=edge_scratch,
                materialize=materialize,
                input_materializer=root_materializer,
            )
            if state != "complete":
                if error is not None:
                    self._edge_errors[edge.target] = error
                return state, None
            prefix, _deep_type, root_type, source_local, conversion_steps = deep
            chain_offset = len(prefix) + 1
        if conversion_steps:
            if root_checksum is None:
                return "unwired", None
            checksum = root_checksum
            recipe = root_checksum
            # One Expression per maximal fusible run (expressions.md, *Fusion*);
            # the anonymous cells inside a run are elided: never built, no
            # checksum held (cells.md, *Anonymous cells, symbols and elision*).
            links = anonymous_links(root_type, source_local, conversion_steps)
            runs = fusible_runs(root_type, links)
            for number, (input_type, path, next_type, last_link) in enumerate(runs):
                last = number == len(runs) - 1
                previous = recipe if isinstance(recipe, Expression) else None
                try:
                    recipe = Expression(
                        recipe, path=_path_string(path),
                        input_celltype=input_type, celltype=next_type,
                    )
                except (TypeError, ValueError) as exc:
                    from .errors import execution_error

                    self._edge_errors[edge.target] = execution_error(exc)
                    return "failed", None
                state, checksum, error = self._projection(
                    checksum, path, input_type, next_type,
                    scratch=edge_scratch if last else True,
                    materialize=materialize if last else False,
                    input_expression=previous,
                    input_materializer=root_materializer if number == 0 else None,
                )
                if state != "complete":
                    if error is not None:
                        self._edge_errors[edge.target] = error
                    return state, None
                if not last:
                    link_type, link_path = links[last_link]
                    symbol = self._anonymous_symbol(
                        edge, (source_node, link_type, link_path),
                        index=chain_offset + last_link,
                    )
                    self._anonymous_current_updates[symbol] = (
                        normalize_checksum(checksum), True,
                    )
            return "complete", checksum
        if source_local:
            return self._projection(
                root_checksum, source_local, root_type,
                projected_celltype(root_type, source_local),
                scratch=edge_scratch,
                materialize=materialize,
                input_materializer=root_materializer,
            )[:2]
        return "complete", root_checksum

    def _apply_pending(self, node, edges, *, join=False):
        states = [self._source_state(edge)[0] for edge in edges]
        reason_for = {
            "miswired": "miswired",
            "blocked-by-miswiring": "blocked-by-miswiring",
            "failed": "blocked-by-error",
            "blocked": "blocked-by-unwired",
            "unwired": "blocked-by-unwired",
        }
        reasons = {}
        scalar_reason = None
        for edge, state in zip(edges, states):
            reason = reason_for.get(state)
            if reason is None:
                continue
            if join:
                _, local = self._graph.resolve_existing(edge.target)
                if local:
                    reasons[local[0]] = reason
            else:
                scalar_reason = reason
        if join:
            node.block_reason = reasons or None
            if "miswired" in reasons.values():
                node.state = "miswired"
            elif "unwired" in reasons.values():
                node.state = "unwired"
            else:
                node.state = "blocked" if reasons else "waiting"
        else:
            node.block_reason = scalar_reason
            node.state = "blocked" if scalar_reason is not None else "waiting"
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
        elif state in {"miswired", "blocked-by-miswiring"}:
            self._replace_current_checksum(next(path for path, candidate in self._graph.nodes.items() if candidate is node), None)
            node.state, node.block_reason = "blocked", "blocked-by-miswiring"
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
        return self._projection(
            node.current_checksum,
            local,
            node.cell_config.celltype,
            target_type=self._celltype_for_path(node_path, local),
            scratch=node.cell_config.scratch,
        )[1]

    def _record_node_error(self, node_path, error):
        self._replace_current_checksum(node_path, None)
        node = self._graph.nodes[node_path]
        node.state, node.block_reason, node.exception = "failed", None, error

    def _get_value(self, node_path, local=(), *, celltype=None):
        checksum = self._get_checksum(node_path, local)
        if checksum is None:
            return None
        node = self._graph.nodes[node_path]
        if celltype is None:
            celltype = self._celltype_for_path(node_path, local)
        try:
            return value_for_checksum(checksum, celltype)
        except CacheMissError:
            raise
        except Exception as exc:
            from .errors import execution_error
            error = execution_error(exc)
            node.state, node.block_reason, node.exception = "failed", None, error
            raise error

    def _get_buffer(self, node_path, local=()):
        checksum = self._get_checksum(node_path, local)
        if checksum is None:
            return None
        node = self._graph.nodes[node_path]
        celltype = self._celltype_for_path(node_path, local)
        try:
            from seamless.checksum.hash_type_validation import validate_deserializable_as
            validate_deserializable_as(checksum, celltype)
            buffer = buffer_for_checksum(checksum)
            validate_deserializable_as(checksum, celltype, buffer=buffer)
            return buffer
        except CacheMissError:
            raise
        except Exception as exc:
            from .errors import execution_error
            error = execution_error(exc)
            node.state, node.block_reason, node.exception = "failed", None, error
            raise error

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

    def _build_cell_expression(self, node_path, local, input_ref, *,
                               _target_celltype=None,
                               _conversion=False, _conversion_steps=()):
        from seamless.cell_class import _UNSET, _typed_input_celltype
        node = self._graph.nodes[node_path]
        node_celltype = self._node_celltype(node_path)
        celltype = _target_celltype or self._celltype_for_path(node_path, local)
        input_type = node_celltype
        if _target_celltype is not None:
            # A handle starts at its parent node.  Its upstream wiring has
            # already produced that node's checksum and is a fusion boundary.
            if input_ref is _UNSET:
                input_ref = node.current_checksum
                input_type = node_celltype
            else:
                input_type = _typed_input_celltype(input_ref) or node_celltype
            return self._build_conversion_expression(
                input_ref, input_type, local, _conversion_steps, celltype,
            )
        if (
            input_ref is _UNSET
            and local
            and not _conversion
            and not _conversion_steps
            and node.kind == "cell"
            and node.current_checksum is not None
        ):
            fused = self._fused_projection_source(node_path, local)
            if fused is not None:
                checksum, input_type, fused_path = fused
                return self._path_expression(checksum, input_type, fused_path, celltype)
        if input_ref is _UNSET:
            if node.cell_root_expression is not None:
                expression = node.cell_root_expression
                if local:
                    return self._path_expression(
                        expression, expression.celltype, tuple(local), celltype,
                    )
                return expression
            incoming = self._incoming_for(node_path)
            if set(incoming) == {()}:
                edge = incoming[()]
                source_path, source_local = self._graph.resolve_existing(edge.source)
                if edge.source_celltype is not None or source_local:
                    # A projection edge that records no celltype -- one saved
                    # and reloaded, or a looked-through single link
                    # (``_incoming_for``) -- builds exactly like a live one.
                    checksum = self._get_checksum(source_path, ())
                    root_type = self._node_celltype(source_path)
                    source_type = self._edge_input_celltype(edge)
                    if edge.source_conversion_steps or edge.source_conversion:
                        steps = edge.source_conversion_steps
                        if edge.source_conversion and not steps:
                            position = 0 if edge.source_conversion_before else len(source_local)
                            steps = ((position, source_type),)
                        if split_deep_step(root_type, source_local, steps) is None:
                            return self._build_edge_recipe(
                                edge, source_path, checksum, root_type,
                                source_local, steps, celltype,
                            )
                        return self._build_conversion_expression(
                            checksum, root_type, source_local, steps, celltype
                        )
                    if source_local:
                        fused = self._fused_projection_source(source_path, source_local)
                        if fused is not None:
                            checksum, input_type, fused_path = fused
                            return self._path_expression(checksum, input_type, fused_path, celltype)
                    upstream = self._incoming_for(source_path).get(())
                    if (source_local and upstream is not None
                            and upstream.source_celltype is not None
                            and upstream.source_celltype != root_type):
                        upstream_type = upstream.source_celltype
                        base = Expression(
                            checksum,
                            input_celltype=upstream_type,
                            celltype=root_type,
                        )
                        return Expression(
                            base,
                            path=_path_string(source_local),
                            input_celltype=root_type,
                            celltype=celltype,
                        )
                    if edge.source_conversion_before and source_local:
                        base = Expression(
                            checksum,
                            input_celltype=root_type,
                            celltype=source_type,
                        )
                        return Expression(
                            base,
                            path=_path_string(source_local),
                            input_celltype=source_type,
                            celltype=celltype,
                        )
                    if source_local:
                        if root_type in _DEEP_CELLTYPES:
                            # The path is read at the deep index's celltype, not
                            # at the member celltype the edge reports.
                            return self._path_expression(checksum, root_type, source_local, celltype)
                        return Expression(
                            checksum,
                            path=_path_string(source_local),
                            input_celltype=source_type,
                            celltype=celltype,
                        )
                    input_ref = checksum
                    input_type = source_type
                else:
                    input_ref = self._build_source_expression(edge.source)
                    input_type = input_ref.celltype
            elif not incoming and node.kind == "cell" and node.cell_root_producer is not None:
                input_ref = node.cell_root_producer.checksum
                input_type = node.cell_root_producer.celltype
            else:
                input_ref = self._get_checksum(node_path, ())
        else:
            input_type = _typed_input_celltype(input_ref) or celltype
        if input_ref is None:
            return Expression(None, input_celltype=None, celltype=celltype)
        if _conversion_steps:
            return self._build_conversion_expression(
                input_ref, input_type, local, _conversion_steps, celltype
            )
        base_celltype = _target_celltype if _conversion and _target_celltype else node_celltype
        if (isinstance(input_ref, Expression) and not local and input_type == base_celltype
                and input_ref.input_celltype in _DEEP_CELLTYPES):
            # A deep step forms no pair, so the Expression constructor will not
            # absorb an identity wrapper into it: the step is the recipe.
            return input_ref
        expression = Expression(input_ref, input_celltype=input_type, celltype=base_celltype)
        if local:
            return self._path_expression(expression, base_celltype, tuple(local), celltype)
        return expression

    def _build_edge_recipe(self, edge, source_path, checksum, root_type, local,
                           steps, final_type):
        """Build a bound edge's recipe from its fusible runs.

        One Expression per run (expressions.md, *Fusion*).  A run after the
        first is rooted at the checksum the Context holds for the previous
        run's last anonymous cell, so that named and anonymous intermediates
        build the same identity (expressions.md, *Identity*); the anonymous
        cells inside a run are elided and never looked up.
        """
        links = anonymous_links(root_type, local, steps)
        runs = fusible_runs(root_type, links)
        current = checksum
        current_type = root_type
        for number, (input_type, path, next_type, _last) in enumerate(runs):
            if number:
                previous_last = runs[number - 1][3]
                link_type, link_path = links[previous_last]
                symbol = self._anonymous_symbol(
                    edge, (source_path, link_type, link_path), index=previous_last,
                )
                held = self._anonymous_current_refholds.get(symbol)
                if held is not None:
                    current = held[0]
            current = Expression(
                current, path=_path_string(path),
                input_celltype=input_type, celltype=next_type,
            )
            current_type = next_type
        if current_type != final_type:
            current = Expression(
                current, input_celltype=current_type, celltype=final_type,
            )
        if isinstance(current, Expression):
            return current
        return Expression(current, input_celltype=root_type, celltype=final_type)

    def _build_conversion_expression(self, checksum, root_type, local, steps, final_type):
        """Build a recipe without evaluating any intermediate link."""
        current = checksum
        current_type = root_type
        path_index = 0
        for position, converted_type in sorted(steps, key=lambda item: item[0]):
            position = min(position, len(local))
            segment = tuple(local[path_index:position])
            if segment:
                next_type = projected_celltype(current_type, segment)
                current = self._path_expression(
                    current, current_type, segment, next_type,
                )
                current_type = next_type
                path_index = position
            if converted_type != current_type:
                current = Expression(
                    current,
                    input_celltype=current_type,
                    celltype=converted_type,
                )
                current_type = converted_type
        segment = tuple(local[path_index:])
        if segment:
            next_type = projected_celltype(current_type, segment)
            current = self._path_expression(
                current, current_type, segment, next_type,
            )
            current_type = next_type
        if current_type != final_type:
            current = Expression(
                current, input_celltype=current_type, celltype=final_type,
            )
        if isinstance(current, Expression):
            return current
        return Expression(current, input_celltype=root_type, celltype=final_type)

    def _build_source_expression(self, source):
        node_path, local = self._graph.resolve_existing(source)
        checksum = self._get_checksum(node_path, ())
        if checksum is None:
            raise ValueUnavailableError(f"Source {source!r} is not current")
        input_celltype = self._node_celltype(node_path)
        return self._path_expression(
            checksum, input_celltype, local, self._celltype_for_path(node_path, local),
        )

    def _fused_projection_source(self, node_path, local):
        """Root the maximal fusible run ending at a projection of ``node_path``.

        Each visited cell is a valid fallback root in its own current state.  The
        walk continues upstream only through a single, same-celltype, non-converting
        root edge; any other edge is a fusion boundary.
        """
        node_path, path = tuple(node_path), tuple(local)
        root = None
        visited = set()
        while node_path not in visited:
            visited.add(node_path)
            node = self._graph.nodes[node_path]
            checksum = node.current_checksum
            if node.kind != "cell" or checksum is None:
                break
            celltype = self._node_celltype(node_path)
            root = checksum, celltype, path

            incoming = self._incoming_for(node_path)
            edge = incoming.get(())
            if edge is None or len(incoming) > 1:
                break
            if edge.source_conversion or edge.source_conversion_steps:
                break

            source_path, source_local = self._graph.resolve_existing(edge.source)
            source_node = self._graph.nodes[source_path]
            if source_node.kind != "cell":
                break
            source_type = edge.source_celltype or self._celltype_for_path(source_path, source_local)
            if source_type != celltype:
                break
            if source_local and self._node_celltype(source_path) in _DEEP_CELLTYPES:
                break

            path = tuple(source_local) + path
            node_path = source_path
        return root

    def _capture_endpoint(self, endpoint):
        node = self._graph.nodes.get(endpoint.node_path)
        if node is None:
            raise StaleWorkflowHandleError(f"Endpoint {endpoint.node_path!r} is stale")
        if endpoint.local_path or endpoint.conversion:
            raise DependencyError(
                "An anonymous or projected workflow handle cannot be captured as a standalone source"
            )
        if node.state == "waiting": raise NotImplementedError("Capturing waiting workflow sources requires future-wired E/T")
        if node.state in {"unwired", "blocked"}: raise ValueError(f"Cannot capture workflow source in state {node.state!r}")
        checksum = self._get_checksum(endpoint.node_path, ())
        if checksum is None:
            raise ValueError("Workflow source has no concrete checksum")
        return Expression(
            checksum,
            path=_path_string(endpoint.local_path),
            input_celltype=self._node_celltype(endpoint.node_path),
            celltype=self._celltype_for_path(endpoint.node_path, endpoint.local_path),
        )

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
        errors = [node.exception]
        errors.extend(error for _, _, error in node.pin_states.values() if error is not None)
        node.pin_read_errors.clear()
        if not any(error is not None for error in errors): return
        for key, (lease, error) in list(self._facts.items()):
            if error is not None and any(error is candidate for candidate in errors):
                self._facts.pop(key)
                if lease is not None: lease._release_refholds()
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
            if current is not None: self._suspend(path)
            return
        # The upstream event: the cell has a concrete checksum again.
        self._release_upstream_holds(path, lambda record: False)
        if current and current.identity_checksum == identity_checksum:
            current.result_checksum = node.current_checksum
            return
        if current is not None:
            self._suspend(path)
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
                entry = {"type": "cell", "path": list(path), "celltype": node.cell_config.celltype, "validator": node.cell_config.validator, "validator_language": node.cell_config.validator_language, "scratch": node.cell_config.scratch, "value": None if node.cell_root_producer is None else {"checksum": node.cell_root_producer.checksum.hex(), "celltype": node.cell_root_producer.celltype}}
            else:
                cfg = node.transformer_config
                entry = {"type": "transformer", "path": list(path), "language": cfg.language, "result_celltype": cfg.celltypes.get("result", "mixed"), "schema": cfg.schema, "compilation": copy.deepcopy(cfg.compilation), "objects": copy.deepcopy(cfg.objects), "header": cfg.header, "call_mode": cfg.call_mode, "pins": {p: {"celltype": cfg.celltypes.get(p, "mixed")} for p in sorted(cfg.pins)}, "optional_pins": sorted(cfg.optional_pins), "checksum": {"code": cfg.code_checksum.hex() if cfg.code_checksum else None}, "code": cfg.code if hasattr(cfg.code, "decode") else None, "meta": copy.deepcopy(cfg.meta), "modules": copy.deepcopy(cfg.modules), "globals": copy.deepcopy(cfg.globals), "environment": copy.deepcopy(cfg.environment), "scratch": cfg.scratch, "local": cfg.local, "direct_print": cfg.direct_print, "producers": {p: {"checksum": q.checksum.hex(), "celltype": q.celltype} for p, q in sorted(node.transformer_pin_producers.items())}}
            if node.mount is not None and node.mount.driver == "file": entry["mount"] = node.mount.to_graph()
            if runtime:
                entry["runtime"] = {"state": node.state, "block_reason": node.block_reason, "checksum": node.current_checksum.hex() if node.current_checksum else None, "exception": type(node.exception).__name__ if node.exception else None, "run": self._runtime_graph_entry(path)}
            nodes.append(entry)
        connections = []
        anonymous_nodes = {}

        def add_anonymous_chain(chain):
            # cells.md, *Which entries are serialized*: an entry is written
            # because an edge, or another written entry, names it.  Symbols
            # were assigned when the edge was added; here they are looked up.
            source_ref = None
            for recipe in chain:
                source_key, celltype, path = recipe
                symbol = self._graph.symbol_of(recipe)
                anonymous_nodes[symbol] = {
                    "source": (
                        {"node": list(source_key)}
                        if isinstance(source_key, tuple)
                        else {"symbol": source_key}
                    ),
                    "celltype": celltype,
                    "path": _path_string(tuple(path)),
                }
                source_ref = {"symbol": symbol}
            return source_ref

        for edge in sorted(self._graph.edges, key=lambda e: (e.source, e.target)):
            if edge.source_conversion or self._graph.resolve_existing(edge.source)[1]:
                # An edge with anonymous links registered their symbols when
                # it was added; a miss means that a call site was missed.
                assert edge.source_chain, (
                    f"edge to {edge.target!r} was added without register_edge_symbols()"
                )
                for recipe in edge.source_chain:
                    self._graph.symbol_of(recipe)
            connection = {"type": "connection", "target": list(edge.target)}
            if edge.deep_member:
                connection["deep_member"] = True
            # cells.md, *Which entries are serialized*: a dummy edge from a
            # single link that reads directly from a named node is saved as
            # the target's own incoming link, which a running Context already
            # looks it through as (``_incoming_for``).  A chain is saved as it
            # is; a path that crosses a deep step is a chain of two links (the
            # step is a fusion barrier).
            if not edge.source_chain or self._graph.collapses_on_save(edge):
                target_node, _ = self._graph.resolve_existing(edge.target)
                if self._graph.nodes[target_node].kind == "cell":
                    source_node, source_local = self._graph.resolve_existing(edge.source)
                    if (
                        source_local
                        or self._graph.nodes[source_node].cell_root_producer is not None
                    ):
                        connection["source"] = list(source_node + source_local)
                    else:
                        connection["source"] = {"node": list(source_node)}
                else:
                    connection["source"] = list(edge.source)
            else:
                connection["source"] = add_anonymous_chain(edge.source_chain)
            connections.append(connection)
        return {
            "__seamless_workflow__": "0.5",
            "nodes": nodes,
            "anonymous_nodes": {key: anonymous_nodes[key] for key in sorted(anonymous_nodes)},
            "connections": connections,
            "params": {},
        }

    def set_graph(self, graph, *, mount_prepared=()):
        # Acquire the staged durable graph before releasing any live roles.
        claims = []
        for path, node in graph.nodes.items():
            if node.cell_root_producer is not None:
                claims.append((node.cell_root_producer.checksum, self._node_scratch(node)))
            claims.extend(
                (producer.checksum, False)
                for producer in node.transformer_pin_producers.values()
            )
            if node.kind == "transformer":
                cfg = node.transformer_config
                if cfg.code_checksum is not None:
                    claims.append((cfg.code_checksum, False))
                claims.extend(
                    (value, False)
                    for value in cfg.modules.values()
                    if isinstance(value, Checksum)
                )
        acquired = []
        try:
            for checksum, scratch in claims:
                checksum.incref_refholder(scratch=scratch)
                acquired.append(checksum)
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
        self._module_refholds = {
            (path, name): (normalize_checksum(module), False)
            for path, node in graph.nodes.items()
            if node.kind == "transformer"
            for name, module in node.transformer_config.modules.items()
            if isinstance(module, Checksum)
        }
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
                    producer.checksum, producer.celltype,
                    scratch=copied_node.cell_config.scratch,
                )
            for pin, producer in list(copied_node.transformer_pin_producers.items()):
                copied_node.transformer_pin_producers[pin] = self._retain_producer(
                    producer.checksum, producer.celltype,
                    scratch=False,
                )
            if copied_node.current_checksum is not None:
                copied_node.current_checksum.incref_refholder(
                    scratch=self._node_scratch(copied_node)
                )
            if copied_node.kind == "transformer":
                self._retain_code_checksum(copied_path, copied_node.transformer_config.code_checksum)
        from dataclasses import replace

        for edge in list(self._graph.edges):
            if edge.source[:len(source_prefix)] == source_prefix and edge.target[:len(source_prefix)] == source_prefix:
                # Keep source_celltype and the conversion fields: the copy
                # converts exactly as the original does.
                copied = replace(
                    edge,
                    source=target_prefix + edge.source[len(source_prefix):],
                    target=target_prefix + edge.target[len(source_prefix):],
                    source_chain=(),
                )
                if edge.source_chain:
                    # The same links, now hanging off the copied source node.
                    source_node, _ = self._graph.resolve_existing(copied.source)
                    links = [(celltype, path) for _, celltype, path in edge.source_chain]
                    copied = replace(
                        copied, source_chain=self._graph.assign_chain(source_node, links)
                    )
                self._graph.edges.append(self._graph.register_edge_symbols(copied))
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
        "close", "_after_turn", "_check_public_caller", "_transformer_config_from_code", "_transformer_config_from_frozen"
    }:
        setattr(Context, _name, controller_method(_method))
Context.compute = _compute
Context.computation = _computation
