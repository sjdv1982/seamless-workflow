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
        self.context._check_public_caller()
        try:
            node = self.context._node_snapshot(self.node_path)
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
        self.context._set_node_config(self.node_path, "celltype", value)

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
        self.context._set_node_config(self.node_path, "target_celltype", value)

    @property
    def validator(self):
        return self._node().cell_config.validator

    @validator.setter
    def validator(self, value):
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        self.context._set_node_config(self.node_path, "validator", value)

    @property
    def validator_language(self):
        return self._node().cell_config.validator_language

    @validator_language.setter
    def validator_language(self, value):
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        self.context._set_node_config(self.node_path, "validator_language", value)

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

    @property
    def state(self):
        return self._node().state

    @property
    def block_reason(self):
        return self._node().block_reason

    @property
    def exception(self):
        node = self._node()
        return node.exception if node.state == "failed" else None

    def derive(self, **updates):
        self._node()
        local = self.local_path
        kwargs = {
            "readonly": self.readonly,
        }
        if "celltype" in updates:
            kwargs["celltype"] = updates["celltype"]
        from seamless import Cell
        expression = self.build(_UNSET)
        result = Cell(expression, celltype=updates.get("celltype", self.celltype),
                      target_celltype=updates.get("target_celltype", self.target_celltype),
                      validator=updates.get("validator", self.validator),
                      validator_language=updates.get("validator_language", self.validator_language))
        return result._workflow_backend if result._workflow_backend is not None else result


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
        self.context._cell_operation(self.node_path, path + (name,), value, detach=True)

    def assign_item(self, owner_path, key, value):
        self._ensure_writable()
        path = self.local_path if isinstance(owner_path, str) else tuple(owner_path)
        if _same_endpoint(value, self._endpoint_for_local(path + (key,))):
            return
        self.context._cell_operation(self.node_path, path + (key,), value, detach=True)

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
        from .ingress import _edit
        _edit(self.context, self.node_path, local, value, operation=operation)

    def build(self, input_ref):
        self._node()
        return self.context._build_cell_expression(self.node_path, self.local_path, input_ref)

    def compute(self, input_ref, *, timeout=None):
        self._node()
        if input_ref is not _UNSET:
            return self.build(input_ref).compute()
        return self.context._compute_cell_endpoint(self.node_path, self.local_path, timeout=timeout)

    def run(self, input_ref):
        self._node()
        if input_ref is not _UNSET:
            return self.build(input_ref).run()
        return self.context._compute_cell_value(self.node_path, self.local_path)

    async def compute_async(self, input_ref, *, timeout=None):
        if input_ref is not _UNSET:
            return await self.build(input_ref).compute_async()
        from .ingress import _wait_async
        lease = await _wait_async(self.context, self.node_path, self.local_path, read=True, barrier=True, timeout=timeout)
        try:
            checksum = lease.checksum
            if checksum is not None and self.local_path:
                from .sidework import evaluate_projection
                import asyncio
                checksum = await asyncio.to_thread(evaluate_projection, checksum, self.local_path, lease.celltype, lease.celltype)
            if checksum is not None: checksum.tempref()
            return checksum
        finally: lease._release_refholds()

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
        self._backend.context._set_node_config(self._backend.node_path, self._field, value, key=str(key))

    def __delitem__(self, key):
        self._backend.context._set_node_config(self._backend.node_path, self._field, None, key=str(key), delete=True)

    def __delattr__(self, name):
        self.__delitem__(name)

    def __repr__(self):
        return repr(self._mapping())


class WorkflowCelltypes(WorkflowMapping):
    def __setitem__(self, key, value):
        self._backend.context._set_node_config(self._backend.node_path, "celltypes", value, key=str(key))


class BoundTransformerBackend:
    def __init__(self, context, node_path: tuple[str, ...]):
        self.context = context
        self.node_path = tuple(node_path)

    def _node(self):
        self.context._check_public_caller()
        try:
            node = self.context._node_snapshot(self.node_path)
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
    def state(self):
        return self._node().state

    @property
    def block_reason(self):
        return self._node().block_reason

    @property
    def exception(self):
        node = self._node()
        return node.exception if node.state == "failed" else None

    @property
    def language(self): return self.cfg.language
    @language.setter
    def language(self, value): self.context._set_node_config(self.node_path, "language", value)
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
    def optional_pins(self, value): self.context._set_node_config(self.node_path, "optional_pins", value)
    @property
    def modules(self): return WorkflowMapping(self, "modules")
    @property
    def globals(self): return WorkflowMapping(self, "globals")
    @property
    def environment(self): return copy.deepcopy(self.cfg.environment)
    @property
    def meta(self): return copy.deepcopy(self.cfg.meta)
    @meta.setter
    def meta(self, value): self.context._set_node_config(self.node_path, "meta", value)
    @property
    def scratch(self): return self.cfg.scratch
    @scratch.setter
    def scratch(self, value): self.context._set_node_config(self.node_path, "scratch", value)
    @property
    def direct_print(self): return self.cfg.direct_print
    @direct_print.setter
    def direct_print(self, value): self.context._set_node_config(self.node_path, "direct_print", value)
    @property
    def local(self): return self.cfg.local
    @local.setter
    def local(self, value): self.context._set_node_config(self.node_path, "local", value)
    @property
    def allow_input_fingertip(self): return bool(self.cfg.meta.get("allow_input_fingertip", False))
    @allow_input_fingertip.setter
    def allow_input_fingertip(self, value):
        self.context._set_node_config(self.node_path, "allow_input_fingertip", value)
    @property
    def driver(self): return bool(self.cfg.meta.get("driver", False))
    @driver.setter
    def driver(self, value): self.context._set_node_config(self.node_path, "driver", value)
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
        return producer.checksum.resolve(producer.celltype)

    def _set_pin(self, pin, value):
        self._node()
        self.context._set_transformer_pin(self.node_path, pin, value)

    def _delete_pin(self, pin):
        self._node()
        self.context._delete_transformer_pin(self.node_path, pin)

    def snapshot_for_call(self):
        return self.context._snapshot_transformer(self.node_path)

    def _workflow_endpoint(self):
        self._node()
        return BoundEndpoint(self.context.top_id, self.node_path, "transformer-result", (), True, False, False)

    def capture_source(self):
        return self.context._capture_endpoint(self._workflow_endpoint())

    def compute(self, timeout=None): return self.context._compute_node(self.node_path, reactive=True, checksum=True, timeout=timeout)
    async def computation(self, timeout=None):
        from .ingress import _wait_async
        lease = await _wait_async(self.context, self.node_path, read=True, barrier=True, timeout=timeout)
        try: return lease.checksum
        finally: lease._release_refholds()
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
