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
    def _input_ref(self):
        if self.input_celltype is None:
            return None
        return self.build(_UNSET)._input_ref

    @property
    def source(self):
        self._node()
        return self.context._public_cell_source(self.node_path, self.local_path)

    @property
    def input_celltype(self):
        self._node()
        if self.readonly:
            return self.celltype
        return self.context._effective_input_celltype(self.node_path)

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
    def scratch(self):
        node = self._node()
        if node.cell_config is None:
            # A transformer result follows its transformer's scratch setting.
            return bool(node.transformer_config.scratch)
        return bool(node.cell_config.scratch)

    @scratch.setter
    def scratch(self, value):
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        self.context._set_node_config(self.node_path, "scratch", value)

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
        value = self.context._get_value(self.node_path, self.local_path, celltype=self.celltype)
        return value.content if self.celltype == "bytes" and hasattr(value, "content") else value

    @property
    def state(self):
        return self._node().state

    @property
    def block_reason(self):
        return self._node().block_reason

    @property
    def exception(self):
        node = self._node()
        return str(node.exception) if node.state == "failed" and node.exception is not None else None

    def derive(self, **updates):
        self._node()
        from seamless.cell_class import _UNSET
        ref = updates.pop("_input_ref", _UNSET)
        if ref is _UNSET:
            result = Cell(source=self.build(ref), celltype=self.celltype,
                          validator=self.validator, validator_language=self.validator_language)
        else:
            recipe = {"checksum": ref} if ref is None or isinstance(ref, Checksum) else {"source": ref}
            result = Cell(**recipe, celltype=self.celltype)
            for component in self.local_path:
                result = result[component]
            result = result.with_validator(self.validator, language=self.validator_language)
        for key, value in updates.items():
            setattr(result, key, value)
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

    def write_value(self, value, *, detach=False):
        self._ensure_writable()
        self.context._cell_operation(self.node_path, self.local_path, value,
                                     detach=detach and not self.local_path)

    def write_checksum(self, checksum, *, input_celltype=None, detach=False):
        self._ensure_writable()
        self.context._cell_operation(self.node_path, self.local_path, checksum,
                                     checksum_rhs=True, input_celltype=input_celltype,
                                     detach=detach and not self.local_path)

    def write_buffer(self, buffer, *, detach=False):
        self._ensure_writable()
        from seamless.cell_class import _checksum_for_buffer
        checksum = _checksum_for_buffer(buffer, self.celltype)
        self.write_checksum(checksum, detach=detach)

    def set(self, value):
        return self.write_value(value)

    def set_checksum(self, checksum, *, input_celltype=None):
        return self.write_checksum(checksum, input_celltype=input_celltype)

    def assign(self, owner_path, name, value):
        self._ensure_writable()
        path = self.local_path if isinstance(owner_path, str) else tuple(owner_path)
        if _same_endpoint(value, self._endpoint_for_local(path + (name,))):
            return
        self.context._cell_operation(self.node_path, path + (name,), value, detach=False)

    def assign_item(self, owner_path, key, value):
        self._ensure_writable()
        path = self.local_path if isinstance(owner_path, str) else tuple(owner_path)
        if _same_endpoint(value, self._endpoint_for_local(path + (key,))):
            return
        self.context._cell_operation(self.node_path, path + (key,), value, detach=False)

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
        value = self.context._compute_cell_value(self.node_path, self.local_path)
        return value.content if self.celltype == "bytes" and hasattr(value, "content") else value

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


class BoundPinBackend:
    def __init__(self, context, node_path, pin):
        self.context, self.node_path, self.pin = context, tuple(node_path), pin

    def _node(self):
        node = self.context._node_snapshot(self.node_path)
        if node.kind != 'transformer' or self.pin not in node.transformer_config.pins:
            raise AttributeError(self.pin)
        return node

    def _read(self, field):
        state, error, source, input_type, lease = self.context._pin_snapshot(self.node_path, self.pin)
        try:
            if field == 'state': return state
            if field == 'exception': return str(error) if error is not None else None
            if field == 'source': return source
            if field == 'input_celltype': return input_type
            if field == 'celltype': return lease.celltype
            checksum = lease.checksum
            if field == 'checksum':
                if checksum is not None: checksum.tempref()
                return checksum
            if checksum is None:
                if error is not None:
                    raise RuntimeError(str(error))
                return None
            if field == 'buffer':
                from seamless.checksum.hash_type_validation import validate_deserializable_as
                validate_deserializable_as(checksum, lease.celltype)
                buffer = checksum.resolve()
                validate_deserializable_as(checksum, lease.celltype, buffer=buffer)
                return buffer
            value = checksum.resolve(lease.celltype)
            return value.content if lease.celltype == 'bytes' and hasattr(value, 'content') else value
        except Exception as exc:
            from seamless import CacheMissError
            if field in {'buffer', 'value'} and lease.checksum is not None and not isinstance(exc, CacheMissError):
                from .errors import execution_error
                identity = (lease.checksum.hex(), input_type, lease.celltype)
                self.context._record_pin_read_error(self.node_path, self.pin, identity, execution_error(exc))
            raise
        finally:
            lease._release_refholds()

    state = property(lambda self: self._read('state'))
    exception = property(lambda self: self._read('exception'))
    source = property(lambda self: self._read('source'))
    input_celltype = property(lambda self: self._read('input_celltype'))
    checksum = property(lambda self: self._read('checksum'))
    buffer = property(lambda self: self._read('buffer'))
    value = property(lambda self: self._read('value'))
    celltype = property(lambda self: self._read('celltype'))

    @celltype.setter
    def celltype(self, value):
        self._node()
        self.context._set_node_config(self.node_path, 'celltypes', value, key=self.pin)

    @property
    def _input_ref(self):
        return self.build()._input_ref

    def build(self, input_ref=_UNSET):
        from seamless import Expression
        self._node()
        if input_ref is not _UNSET:
            raise TypeError('Pin.build does not accept a replacement input')
        snapshot = self.context._snapshot_transformer(self.node_path)
        try:
            expression = snapshot.args.get(self.pin)
            if expression is None:
                return Expression(None, celltype=self.celltype)
            return expression
        finally:
            for lease in snapshot.leases: lease._release_refholds()

    def compute(self, input_ref=_UNSET, *, timeout=None):
        if input_ref is not _UNSET: raise TypeError('Pin.compute does not accept a replacement input')
        from .ingress import _wait
        self._node()
        _wait(self.context, self.node_path, barrier=True, timeout=timeout)
        return self.checksum

    async def compute_async(self, input_ref=_UNSET, *, timeout=None):
        if input_ref is not _UNSET: raise TypeError('Pin.compute does not accept a replacement input')
        from .ingress import _wait_async
        self._node()
        await _wait_async(self.context, self.node_path, barrier=True, timeout=timeout)
        return self.checksum

    def run(self, input_ref=_UNSET):
        self.compute(input_ref)
        return self.value

    def fingertip(self):
        checksum = self.checksum
        return None if checksum is None else checksum.fingertip_sync()

    def clear_exception(self):
        self._node()
        return self.context._clear_exception(self.node_path)

    def write_value(self, value, *, detach=False):
        self._node()
        self.context._set_transformer_pin(self.node_path, self.pin, value, detach=detach)

    def write_checksum(self, checksum, *, input_celltype=None, detach=False):
        self._node()
        self.context._set_transformer_pin(self.node_path, self.pin, checksum,
            input_celltype=input_celltype, detach=detach, checksum_rhs=True)

    def write_buffer(self, buffer, *, detach=False):
        from seamless.cell_class import _checksum_for_buffer
        self._node()
        if not detach: self.context._check_authority(self.node_path, (self.pin,))
        self.write_checksum(_checksum_for_buffer(buffer, self.celltype), detach=detach)


class WorkflowTransformerPins:
    def __init__(self, backend):
        object.__setattr__(self, "_backend", backend)

    def __dir__(self):
        return sorted(set(super().__dir__()) | self._backend.cfg.pins)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __getitem__(self, key):
        from seamless_transformer import Pin
        backend = BoundPinBackend(self._backend.context, self._backend.node_path, str(key))
        backend._node()
        return Pin._from_backend(backend)

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

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(self._mapping()))

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
        node = self._node()
        if node.state not in {'miswired', 'unwired', 'blocked', 'waiting'}:
            return None
        return dict(node.pin_block_reasons)

    @property
    def exception(self):
        node = self._node()
        return str(node.exception) if node.state in {"failed", "blocked"} and node.exception is not None else None

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
