"""Canonical builder backends owned by the workflow Context."""

from __future__ import annotations

import copy
import operator
from typing import Any

from seamless import Cell, Checksum
from seamless.cell_class import _UNSET, append_item_path, append_slice_path
from seamless_transformer.builder_snapshot import TransformerBuilderSnapshot

from .endpoints import BoundEndpoint
from .errors import ReadOnlyEndpointError, StaleWorkflowHandleError


def _path_string(path: tuple[Any, ...]) -> str:
    result = ""
    for component in path:
        if isinstance(component, slice):
            result = append_slice_path(result, component.start, component.stop, component.step)
        else:
            result = append_item_path(result, component)
    return result


class BoundCellBackend:
    def __init__(self, context, node_path: tuple[str, ...], local_path=(), *, readonly=False):
        self.context = context
        self.node_path = tuple(node_path)
        self.local_path = tuple(local_path)
        self.readonly = bool(readonly)

    def _node(self):
        try:
            node = self.context._graph.nodes[self.node_path]
        except KeyError as exc:
            raise StaleWorkflowHandleError(
                f"Cell endpoint {self.node_path!r} is stale"
            ) from exc
        if node.kind != "cell" and not self.readonly:
            raise StaleWorkflowHandleError(f"Cell endpoint {self.node_path!r} is stale")
        return node

    @property
    def path(self):
        self._node()
        return _path_string(self.local_path)

    path_python = path

    @property
    def input_ref(self):
        self._node()
        return self.context._get_checksum(self.node_path, self.local_path)

    @property
    def celltype(self):
        node = self._node()
        if self.readonly:
            return node.transformer_config.celltypes.get("result", "mixed")
        return node.cell_config.celltype

    @celltype.setter
    def celltype(self, value):
        self._node()
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        self.context._set_cell_config(self.node_path, celltype=value)

    @property
    def target_celltype(self):
        node = self._node()
        if self.readonly:
            return node.transformer_config.celltypes.get("result", "mixed")
        return node.cell_config.target_celltype

    @target_celltype.setter
    def target_celltype(self, value):
        self._node()
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        self.context._set_cell_config(self.node_path, target_celltype=value)

    @property
    def validator(self):
        return self._node().cell_config.validator

    @validator.setter
    def validator(self, value):
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        self.context._set_cell_config(self.node_path, validator=value)

    @property
    def validator_language(self):
        return self._node().cell_config.validator_language

    @validator_language.setter
    def validator_language(self, value):
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        self.context._set_cell_config(self.node_path, validator_language=value)

    @property
    def checksum(self):
        self._node()
        return self.context._get_checksum(self.node_path, self.local_path)

    @property
    def buffer(self):
        self._node()
        return self.context._get_buffer(self.node_path, self.local_path)

    @property
    def value(self):
        self._node()
        return self.context._get_value(
            self.node_path,
            self.local_path,
            celltype=self.celltype,
        )

    def derive(self, **updates):
        self._node()
        local = self.local_path
        kwargs = {
            "readonly": self.readonly,
        }
        if "celltype" in updates:
            kwargs["celltype"] = updates["celltype"]
        result = type(self)(self.context, self.node_path, local, readonly=self.readonly)
        # Configuration derives are kept on a canonical Cell object by the
        # Context backend; path derivation is handled by derive_item/slice.
        for key, value in updates.items():
            if key in {"celltype", "target_celltype", "validator", "validator_language"}:
                setattr(result, key, value)
            elif key == "input_ref":
                raise AttributeError("input_ref is standalone-only for bound Cells")
        return result

    def derive_item(self, key):
        self._node()
        return type(self)(self.context, self.node_path, self.local_path + (key,), readonly=self.readonly)

    def derive_slice(self, start=None, stop=None, step=None):
        self._node()
        return type(self)(
            self.context,
            self.node_path,
            self.local_path + (slice(start, stop, step),),
            readonly=self.readonly,
        )

    def _ensure_writable(self):
        self._node()
        if self.readonly:
            raise ReadOnlyEndpointError(
                f"Transformer result projection {self.path!r} is read-only"
            )

    def set(self, value):
        self._ensure_writable()
        self.context._cell_operation(self.node_path, self.local_path, value)

    def set_checksum(self, checksum):
        self._ensure_writable()
        self.context._cell_operation(
            self.node_path, self.local_path, Checksum(checksum), checksum_rhs=True
        )

    def assign(self, owner_path, name, value):
        self._ensure_writable()
        path = self.local_path if isinstance(owner_path, str) else tuple(owner_path)
        if _same_endpoint(value, self._endpoint_for_local(path + (name,))):
            return
        self.context._cell_operation(self.node_path, path + (name,), value)

    def assign_item(self, owner_path, key, value):
        self._ensure_writable()
        path = self.local_path if isinstance(owner_path, str) else tuple(owner_path)
        if _same_endpoint(value, self._endpoint_for_local(path + (key,))):
            return
        self.context._cell_operation(self.node_path, path + (key,), value)

    def delete(self, owner_path, name):
        self._ensure_writable()
        path = self.local_path if isinstance(owner_path, str) else tuple(owner_path)
        self.context._cell_delete_path(self.node_path, path + (name,))

    def delete_item(self, owner_path, key):
        self._ensure_writable()
        path = self.local_path if isinstance(owner_path, str) else tuple(owner_path)
        self.context._cell_delete_path(self.node_path, path + (key,))

    def augmented(self, path, operation, value):
        self._ensure_writable()
        local = self.local_path if isinstance(path, str) else tuple(path)
        current = self.context._get_value(self.node_path, local, celltype=self.celltype)
        if current is None:
            raise TypeError(f"Cannot apply {operation} to an empty Cell endpoint")
        result = getattr(operator, operation)(current, value)
        self.context._cell_operation(self.node_path, local, result)

    def build(self, input_ref):
        self._node()
        return self.context._build_cell_expression(self.node_path, self.local_path, input_ref)

    def compute(self, input_ref):
        self._node()
        if input_ref is not _UNSET:
            return self.build(input_ref).compute()
        return self.context._compute_cell_endpoint(self.node_path, self.local_path)

    def run(self, input_ref):
        self._node()
        if input_ref is not _UNSET:
            return self.build(input_ref).run()
        return self.context._compute_cell_value(self.node_path, self.local_path)

    async def compute_async(self, input_ref):
        if input_ref is not _UNSET:
            return await self.build(input_ref).compute_async()
        return self.context._compute_cell_endpoint(self.node_path, self.local_path)

    def prune(self):
        self._node()
        return self.context.prune(self.node_path)

    def clear_exception(self):
        self._node()
        return self.context._clear_exception(self.node_path)

    def _workflow_endpoint(self):
        self._node()
        kind = "transformer-result" if self.readonly else (
            "cell-result" if not self.local_path else "cell-subvalue"
        )
        return BoundEndpoint(
            self.context.top_id,
            self.node_path,
            kind,
            self.local_path,
            can_source=True,
            can_target=not self.readonly,
            can_set=not self.readonly,
        )

    def _endpoint_for_local(self, local):
        kind = "transformer-result" if self.readonly else (
            "cell-result" if not local else "cell-subvalue"
        )
        return BoundEndpoint(
            self.context.top_id,
            self.node_path,
            kind,
            tuple(local),
            True,
            not self.readonly,
            not self.readonly,
        )

    def capture_source(self):
        return self.context._capture_endpoint(self._workflow_endpoint())


class WorkflowTransformerPins:
    def __init__(self, backend):
        object.__setattr__(self, "_backend", backend)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __getitem__(self, key):
        return self._backend._get_pin(str(key))

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self[name] = value

    def __setitem__(self, key, value):
        self._backend._set_pin(str(key), value)

    def __delattr__(self, name):
        if name.startswith("_"):
            object.__delattr__(self, name)
        else:
            del self[name]

    def __delitem__(self, key):
        self._backend._delete_pin(str(key))


class WorkflowMapping:
    def __init__(self, backend, field):
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_field", field)

    def _mapping(self):
        self._backend._node()
        return getattr(self._backend.cfg, self._field)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return copy.deepcopy(self._mapping()[name])
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key):
        return copy.deepcopy(self._mapping()[str(key)])

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self[name] = value

    def __setitem__(self, key, value):
        self._mapping()[str(key)] = copy.deepcopy(value)
        self._backend.context._derive_all()

    def __delitem__(self, key):
        self._mapping().pop(str(key), None)
        self._backend.context._derive_all()

    def __delattr__(self, name):
        self.__delitem__(name)

    def __repr__(self):
        return repr(self._mapping())


class WorkflowCelltypes(WorkflowMapping):
    def __setitem__(self, key, value):
        cfg = self._backend.cfg
        key = str(key)
        cfg.celltypes[key] = str(value)
        if key != "result":
            cfg.pins.add(key)
        self._backend.context._derive_all()


class BoundTransformerBackend:
    def __init__(self, context, node_path: tuple[str, ...]):
        self.context = context
        self.node_path = tuple(node_path)

    def _node(self):
        try:
            node = self.context._graph.nodes[self.node_path]
        except KeyError as exc:
            raise StaleWorkflowHandleError(
                f"Transformer endpoint {self.node_path!r} is stale"
            ) from exc
        if node.kind != "transformer":
            raise StaleWorkflowHandleError(f"Transformer endpoint {self.node_path!r} is stale")
        return node

    @property
    def cfg(self):
        return self._node().transformer_config

    @property
    def pin_names(self):
        return set(self.cfg.pins)

    @property
    def language(self): return self.cfg.language
    @language.setter
    def language(self, value): self.cfg.language = "python" if value is None else value; self.context._derive_all()
    @property
    def code(self): return self.cfg.code
    @code.setter
    def code(self, value): self.context._set_transformer_code(self.node_path, value)
    @property
    def pins(self): return WorkflowTransformerPins(self)
    @property
    def args(self): return self.pins
    @property
    def celltypes(self): return WorkflowCelltypes(self, "celltypes")
    @property
    def optional_pins(self): return set(self.cfg.optional_pins)
    @optional_pins.setter
    def optional_pins(self, value): self.cfg.optional_pins = set(value or ()); self.context._derive_all()
    @property
    def modules(self): return WorkflowMapping(self, "modules")
    @property
    def globals(self): return WorkflowMapping(self, "globals")
    @property
    def environment(self): return copy.deepcopy(self.cfg.environment)
    @property
    def meta(self): return copy.deepcopy(self.cfg.meta)
    @meta.setter
    def meta(self, value): self.cfg.meta.update(copy.deepcopy(value)); self.context._derive_all()
    @property
    def scratch(self): return self.cfg.scratch
    @scratch.setter
    def scratch(self, value): self.cfg.scratch = bool(value); self.context._derive_all()
    @property
    def direct_print(self): return self.cfg.direct_print
    @direct_print.setter
    def direct_print(self, value): self.cfg.direct_print = bool(value); self.context._derive_all()
    @property
    def local(self): return self.cfg.local
    @local.setter
    def local(self, value): self.cfg.local = value; self.cfg.meta["local"] = value; self.context._derive_all()
    @property
    def allow_input_fingertip(self): return bool(self.cfg.meta.get("allow_input_fingertip", False))
    @allow_input_fingertip.setter
    def allow_input_fingertip(self, value):
        if value: self.cfg.meta["allow_input_fingertip"] = True
        else: self.cfg.meta.pop("allow_input_fingertip", None)
    @property
    def driver(self): return bool(self.cfg.meta.get("driver", False))
    @driver.setter
    def driver(self, value): self.cfg.meta["driver"] = bool(value)
    @property
    def result(self): return Cell._from_backend(BoundCellBackend(self.context, self.node_path, (), readonly=True))
    @property
    def call_mode(self): return self.cfg.call_mode

    def _get_pin(self, pin):
        self._node()
        if pin not in self.cfg.pins:
            raise AttributeError(pin)
        edge = self.context._incoming_edge(self.node_path, (pin,))
        if edge is not None:
            return self.context._public_source(edge.source)
        producer = self.cfg and self._node().transformer_pin_producers.get(pin)
        if producer is None:
            return None
        return self.context._value_from_producer(producer)

    def _set_pin(self, pin, value):
        self._node()
        self.context._set_transformer_pin(self.node_path, pin, value)

    def _delete_pin(self, pin):
        self._node()
        self.context._delete_transformer_pin(self.node_path, pin)

    def snapshot_for_call(self):
        node = self._node()
        cfg = node.transformer_config
        args = {}
        for pin in cfg.pins:
            edge = self.context._incoming_edge(self.node_path, (pin,))
            if edge is not None:
                args[pin] = self.context._build_source_expression(edge.source)
            elif pin in node.transformer_pin_producers:
                args[pin] = self.context._value_from_producer(node.transformer_pin_producers[pin])
        import inspect
        return TransformerBuilderSnapshot(
            codebuf=cfg.code,
            language=cfg.language,
            celltypes=copy.deepcopy(cfg.celltypes),
            optional_pins=frozenset(cfg.optional_pins),
            args=args,
            modules=copy.deepcopy(cfg.modules),
            globals=copy.deepcopy(cfg.globals),
            meta=copy.deepcopy(cfg.meta),
            environment=copy.deepcopy(cfg.environment),
            scratch=cfg.scratch,
            direct_print=cfg.direct_print,
            local=cfg.local,
            call_mode=cfg.call_mode,
            callable=cfg.callable,
            signature=inspect.signature(cfg.callable) if callable(cfg.callable) else None,
        )

    def _workflow_endpoint(self):
        self._node()
        return BoundEndpoint(self.context.top_id, self.node_path, "transformer-result", (), True, False, False)

    def capture_source(self):
        return self.context._capture_endpoint(self._workflow_endpoint())

    def compute(self): return self.context._compute_node(self.node_path, reactive=True, checksum=True)
    def run(self): return self.context._compute_node(self.node_path, reactive=True, checksum=False)
    async def task(self): return self.run()
    def prune(self): self._node(); return self.context.prune(self.node_path)
    def clear_exception(self): self._node(); return self.context._clear_exception(self.node_path)


__all__ = [
    "BoundCellBackend",
    "BoundTransformerBackend",
    "WorkflowCelltypes",
    "WorkflowMapping",
    "WorkflowTransformerPins",
]


def _same_endpoint(value, endpoint):
    method = getattr(value, "_workflow_endpoint", None)
    if not callable(method):
        return False
    try:
        return method() == endpoint
    except Exception:
        return False
