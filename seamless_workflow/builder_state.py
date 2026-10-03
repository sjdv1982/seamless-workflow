"""Canonical builder backends owned by the workflow Context."""

from __future__ import annotations

import copy
import operator
from typing import Any

from seamless import Cell, Checksum, Expression
from seamless.cell_class import _UNSET, append_item_path, append_slice_path
from seamless_transformer.frozen_transformer import FrozenTransformer

from .endpoints import BoundEndpoint
from .errors import AuthorityError, DependencyError, ReadOnlyEndpointError, StaleWorkflowHandleError
from .graph import deep_recipe_error


def _path_string(path: tuple[Any, ...]) -> str:
    result = ""
    for component in path:
        if isinstance(component, slice):
            result = append_slice_path(result, component.start, component.stop, component.step)
        else:
            result = append_item_path(result, component)
    return result


_DEEP_CELLTYPES = frozenset({"deepcell", "deepfolder", "folder"})


def _member_celltype(celltype: str, component) -> str:
    """The celltype one projection step below ``celltype``.

    A slice keeps the celltype; an item one step below a deep celltype is the
    member celltype (``mixed`` for ``deepcell``, ``bytes`` for ``deepfolder``
    and ``folder``); any other item keeps the celltype.
    """
    if isinstance(component, slice):
        return celltype
    if celltype == "deepcell":
        return "mixed"
    if celltype in ("deepfolder", "folder"):
        return "bytes"
    return celltype


class BoundCellBackend:
    def __init__(self, context, node_path: tuple[str, ...], local_path=(), *, readonly=False,
                 projected_celltype=None, conversion=False, conversion_before=False,
                 conversion_steps=()):
        self.context = context
        self.node_path = tuple(node_path)
        self.local_path = tuple(local_path)
        self.readonly = bool(readonly)
        self._handle_id = object()
        self._conversion = bool(conversion)
        self._conversion_before = bool(conversion_before)
        conversion_steps = tuple(conversion_steps)
        if self._conversion and not conversion_steps and projected_celltype is not None:
            # A single conversion given without its position.
            position = 0 if self._conversion_before else len(self.local_path)
            conversion_steps = ((position, projected_celltype),)
        # The handle's explicit links: its path, and the conversions written
        # with ``as_celltype`` at their path positions.  Its celltype is not
        # stored: it follows the parent (``celltype``).
        self._conversion_steps = conversion_steps
        self._result_lease = None
        # Handle-local memo and failure of an anonymous or projection handle,
        # keyed by the parent's (checksum, celltype) (_handle_sync).
        self._handle_key = None
        self._handle_result = None
        self._handle_error = None

    def _hold_result(self, checksum):
        lease = self._result_lease
        if lease is not None and checksum is not None and lease.checksum == checksum:
            return checksum
        if lease is not None:
            lease._release_refholds()
            self._result_lease = None
        needs_result_claim = bool(
            self.local_path
            or self._conversion
            or self._conversion_before
            or self._conversion_steps
        )
        if checksum is not None and needs_result_claim:
            from .sidework import Lease
            self._result_lease = Lease(checksum, role="result")
        return checksum

    # --- Reads through an anonymous or projection handle ----------------------
    #
    # cells.md, *Anonymous and projection handles*: such a handle reads like an
    # unbound Cell over its parent's checksum.  Every pulling read builds the
    # handle's own Expression -- all of its links, paths and conversions, in
    # syntax order, fused as far as the Expression constructor allows -- over
    # the parent's checksum, and evaluates it with the standalone getter's
    # machinery (`_available_input_checksum`), placed by the Context's
    # `expression_execution`.  It never waits on the parent.  The memo and the
    # failure live on the handle, keyed by the parent's checksum and celltype.

    def _is_handle(self):
        return bool(self.local_path or self._conversion or self._conversion_steps)

    def _handle_parent(self):
        """Snapshot the parent's checksum: a bound attribute read that never waits."""
        return self.context._read_snapshot(self.node_path)

    def _handle_miswired(self, parent_celltype):
        # A live handle follows its parent (cells.md, *The input*: "a retype
        # never leaves a bound handle `miswired`"), so the ordinary wiring rule
        # cannot fail on it.  A deep link outside the deep tables is still
        # ill-formed (cells.md, *Connecting*; deep-celltypes.md, *Paths*).
        return deep_recipe_error(parent_celltype, self.local_path, self._conversion_steps) is not None

    def _handle_links(self):
        """The handle's links in syntax order: ``(True, component)`` for a path
        step, ``(False, celltype)`` for an ``as_celltype`` conversion."""
        links, index = [], 0
        for position, celltype in self._conversion_steps:
            while index < min(position, len(self.local_path)):
                links.append((True, self.local_path[index]))
                index += 1
            links.append((False, celltype))
        links.extend((True, component) for component in self.local_path[index:])
        return links

    def _handle_celltype(self, parent_celltype):
        """The handle's celltype over a parent of ``parent_celltype``.

        cells.md, *The input*: "A bound handle's `celltype` follows its
        parent."  A path step keeps the celltype, except one step below a deep
        celltype, where it is the member celltype; a conversion written with
        ``as_celltype`` keeps its explicit target.
        """
        current = parent_celltype
        for is_path, item in self._handle_links():
            current = _member_celltype(current, item) if is_path else item
        return current

    def _handle_expression(self, parent_checksum, parent_celltype):
        """Build the handle's Expression over the parent's checksum.

        Each link becomes one Expression whose input is the previous one; the
        Expression constructor fuses path into path and a pathless,
        checksum-preserving conversion into a following path, and keeps every
        other pair a chain (expressions.md, *Fusion*).  Returns the bare parent
        checksum when no link applies anything (the dummy Expression).
        """
        current, current_type = parent_checksum, parent_celltype
        for is_path, item in self._handle_links():
            if is_path:
                next_type = _member_celltype(current_type, item)
                current = Expression(
                    current, path=_path_string((item,)),
                    input_celltype=current_type, celltype=next_type,
                )
                current_type = next_type
            elif item != current_type:
                current = Expression(current, input_celltype=current_type, celltype=item)
                current_type = item
        # ``current_type`` is now the handle's celltype over this parent
        # (``_handle_celltype``): the links are all there is to the recipe.
        return current

    def _handle_sync(self, lease):
        """Discard the memo and failure when the parent changed (passive)."""
        key = None if lease.checksum is None else (lease.checksum.hex(), lease.celltype)
        if key != self._handle_key:
            self._handle_key = key
            self._handle_result = None
            self._handle_error = None

    def _handle_is_dummy(self, parent_celltype):
        current_type = parent_celltype
        for is_path, item in self._handle_links():
            if is_path:
                return False
            current_type = item
        return current_type == parent_celltype

    def _handle_pull(self, *, compute=False):
        """Pull the handle's checksum over the parent's current checksum.

        Standalone resolution order (cells.md, *Reads*): the dummy Expression,
        then the process cache, the database, an in-flight evaluation, local
        evaluation, dispatch.  ``compute`` re-evaluates after a recorded
        failure, as a standalone ``compute()`` does; a plain read reports the
        sticky failure as ``None`` until ``clear_exception()``.
        """
        from seamless.cell_class import _available_input_checksum
        from seamless.checksum.null import canonicalize_checksum
        from seamless.error_envelope import RunningLoopRefusal, execution_error

        lease = self._handle_parent()
        try:
            self._handle_sync(lease)
            parent = lease.checksum
            if parent is None or self._handle_miswired(lease.celltype):
                # No parent checksum: return None without waiting, raising or
                # recording anything.
                return None
            if self._handle_error is not None and not compute:
                return None
            if self._handle_result is not None:
                return self._handle_result
            try:
                built = self._handle_expression(parent, lease.celltype)
                if isinstance(built, Checksum):
                    result = canonicalize_checksum(built, self._handle_celltype(lease.celltype))
                else:
                    # A handle starts non-scratch (cells.md, *Scratch policy*).
                    result = _available_input_checksum(
                        built, scratch=False,
                        execution=self.context._expression_execution,
                    )
            except RunningLoopRefusal:
                # Not a failure: nothing recorded, the handle stays waiting.
                return None
            except Exception as exc:
                self._handle_error = execution_error(exc)
                self._handle_result = None
                return None
            self._handle_error = None
            self._handle_result = result
            return result
        finally:
            lease._release_refholds()

    def _handle_compute(self, timeout=None):
        import asyncio
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if timeout is None or in_loop:
            result = self._handle_pull(compute=True)
        else:
            # `timeout` bounds only the wait for the handle's own evaluation.
            from concurrent.futures import ThreadPoolExecutor
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(self._handle_pull, compute=True)
            finally:
                executor.shutdown(wait=False)
            try:
                result = future.result(timeout=timeout)
            except TimeoutError:
                raise TimeoutError(
                    f"Handle evaluation did not finish within {timeout} seconds"
                ) from None
        if result is not None:
            result.tempref()
        return self._hold_result(result)

    async def _handle_compute_async(self, timeout=None):
        import asyncio
        work = asyncio.to_thread(self._handle_pull, compute=True)
        if timeout is not None:
            work = asyncio.wait_for(work, timeout)
        result = await work
        if result is not None:
            result.tempref()
        return self._hold_result(result)

    def _handle_state(self):
        lease = self._handle_parent()
        try:
            if self._handle_miswired(lease.celltype):
                return "miswired"
            self._handle_sync(lease)
            if lease.checksum is None:
                parent_state = self.context._graph.nodes[self.node_path].state
                return "unwired" if parent_state == "unwired" else "waiting"
            if self._handle_error is not None:
                return "failed"
            if self._handle_result is not None or self._handle_is_dummy(lease.celltype):
                return "complete"
            return "waiting"
        finally:
            lease._release_refholds()

    def _handle_exception(self):
        lease = self._handle_parent()
        try:
            self._handle_sync(lease)
        finally:
            lease._release_refholds()
        return None if self._handle_error is None else str(self._handle_error)

    def _handle_buffer(self):
        checksum = self.checksum
        if checksum is None:
            return None
        # A materialization failure is raised, never recorded (cells.md,
        # *`.buffer` and `.value`*).
        from seamless.buffer_class import Buffer
        from seamless.checksum.hash_type_validation import validate_deserializable_as
        celltype = self.celltype
        mapped = Buffer._map_celltype(celltype)
        validate_deserializable_as(checksum, mapped)
        buffer = checksum.resolve()
        validate_deserializable_as(checksum, mapped, buffer=buffer)
        if celltype in _DEEP_CELLTYPES:
            buffer.get_value(celltype)
        return buffer

    def _handle_value(self):
        checksum = self.checksum
        if checksum is None:
            return None
        celltype = self.celltype
        value = checksum.resolve(celltype)
        return value.content if celltype == "bytes" and hasattr(value, "content") else value

    def _handle_run(self):
        result = self._handle_compute()
        if result is None:
            if self._handle_error is not None:
                raise self._handle_error
            return None
        return self._handle_value()

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
        if self.local_path:
            return self.context._input_celltype_for_path(self.node_path, self.local_path)
        if self._conversion:
            return self.context._node_snapshot(self.node_path).cell_config.celltype
        return self.context._effective_input_celltype(self.node_path)

    @property
    def celltype(self):
        node = self._node()
        if node.kind == "cell":
            parent_celltype = node.cell_config.celltype
        else:
            parent_celltype = node.transformer_config.celltypes.get("result", "mixed")
        # cells.md, *The input*: "A bound handle's `celltype` follows its
        # parent.  Retyping the parent retypes every live handle over it";
        # one step below a deep parent it is the member celltype, and an
        # explicit ``as_celltype`` keeps its target.  Only an edge fixes a
        # celltype, in the anonymous cell it creates.
        return self._handle_celltype(parent_celltype)

    @celltype.setter
    def celltype(self, value):
        self._node()
        if self.readonly:
            raise ReadOnlyEndpointError("Transformer result is read-only")
        if self.local_path and value != self.celltype:
            raise TypeError(
                "Cannot implicitly convert behind a projection; use as_celltype() "
                "before or after projecting"
            )
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
        if self.local_path or self._conversion:
            return False
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
        if self._is_handle():
            return self._hold_result(self._handle_pull())
        if self.state == "miswired":
            return self._hold_result(None)
        return self._hold_result(self.context._get_checksum(self.node_path))

    @property
    def buffer(self):
        self._node()
        if self._is_handle():
            return self._handle_buffer()
        if self.state == "miswired":
            return None
        return self.context._get_buffer(self.node_path)

    @property
    def value(self):
        self._node()
        if self._is_handle():
            return self._handle_value()
        if self.state == "miswired":
            return None
        value = self.context._get_value(self.node_path, celltype=self.celltype)
        return value.content if self.celltype == "bytes" and hasattr(value, "content") else value

    @property
    def state(self):
        node = self._node()
        if self._is_handle():
            return self._handle_state()
        return node.state

    @property
    def block_reason(self):
        return self._node().block_reason

    @property
    def exception(self):
        node = self._node()
        if self._is_handle():
            return self._handle_exception()
        return str(node.exception) if node.state == "failed" and node.exception is not None else None

    def derive(self, **updates):
        self._node()
        from seamless.cell_class import _UNSET
        ref = updates.pop("_input_ref", _UNSET)
        if ref is _UNSET and set(updates) == {"celltype"}:
            conversion_steps = self._conversion_steps + ((len(self.local_path), updates["celltype"]),)
            return type(self)(
                self.context,
                self.node_path,
                self.local_path,
                readonly=self.readonly,
                projected_celltype=updates["celltype"],
                conversion=True,
                conversion_before=(self._conversion_before if self._conversion else not self.local_path),
                conversion_steps=conversion_steps,
            )
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
            if key == "celltype":
                result = result.as_celltype(value)
            else:
                setattr(result, key, value)
        result.scratch = self.scratch
        return result

    def derive_item(self, key):
        self._node()
        # The child's celltype is not fixed here: it follows the parent, and
        # one step below a deep celltype it is the member celltype, also after
        # an as_celltype (cells.md, *The input*; deep-celltypes.md, *Handles
        # and writes one step below a deep parent*).
        return type(self)(
            self.context, self.node_path, self.local_path + (key,), readonly=self.readonly,
            conversion=self._conversion,
            conversion_before=self._conversion_before,
            conversion_steps=self._conversion_steps,
        )

    def derive_slice(self, start=None, stop=None, step=None):
        self._node()
        return type(self)(
            self.context,
            self.node_path,
            self.local_path + (slice(start, stop, step),),
            readonly=self.readonly,
            conversion=self._conversion,
            conversion_before=self._conversion_before,
            conversion_steps=self._conversion_steps,
        )

    def _ensure_writable(self):
        self._node()
        if self.readonly:
            raise ReadOnlyEndpointError(
                f"Transformer result projection {self.path!r} is read-only"
            )
        if self._conversion:
            raise AuthorityError("A converted Cell handle cannot be written")

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
        return self.context._build_cell_expression(
            self.node_path,
            self.local_path,
            input_ref,
            # A handle's celltype follows its parent; it is read now.
            _target_celltype=self.celltype if self._is_handle() else None,
            _conversion=self._conversion_before,
            _conversion_steps=self._conversion_steps,
        )

    def compute(self, input_ref, *, timeout=None):
        self._node()
        if input_ref is not _UNSET:
            return self.build(input_ref).compute()
        if self._is_handle():
            return self._handle_compute(timeout=timeout)
        return self.context._compute_cell_endpoint(self.node_path, timeout=timeout)

    def run(self, input_ref):
        self._node()
        if input_ref is not _UNSET:
            return self.build(input_ref).run()
        if self._is_handle():
            return self._handle_run()
        value = self.context._compute_cell_value(self.node_path)
        if value is None:
            error = self.context._node_snapshot(self.node_path).exception
            if error is not None:
                raise error
        return value.content if self.celltype == "bytes" and hasattr(value, "content") else value

    async def compute_async(self, input_ref, *, timeout=None):
        if input_ref is not _UNSET:
            return await self.build(input_ref).compute_async()
        if self._is_handle():
            self._node()
            return await self._handle_compute_async(timeout=timeout)
        from .ingress import _wait_async
        lease = await _wait_async(
            self.context,
            self.node_path,
            (),
            read=True,
            barrier=True,
            timeout=timeout,
            return_none_on_incomplete=True,
        )
        if lease is None:
            return None
        try:
            checksum = lease.checksum
            if checksum is not None: checksum.tempref()
            return checksum
        finally: lease._release_refholds()

    def prune(self):
        self._node()
        return self.context.prune(self.node_path)

    def clear_exception(self):
        self._node()
        if self._is_handle():
            # The failure lives on the handle; clearing it is the explicit retry.
            self._handle_error = None
            return None
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
            celltype=self.celltype,
            conversion=self._conversion,
            conversion_before=self._conversion_before,
            conversion_steps=self._conversion_steps,
        )


    def _workflow_validate_source(self):
        if self.local_path or self._conversion:
            raise DependencyError(
                "An anonymous or projected workflow handle cannot be used as a standalone source"
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
            self.celltype,
            self._conversion,
            self._conversion_before,
            self._conversion_steps,
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
        frozen = self.context._freeze_transformer(self.node_path)
        try:
            expression = frozen.args.get(self.pin)
            if expression is None:
                return Expression(None, celltype=self.celltype)
            return expression
        finally:
            for lease in frozen.leases: lease._release_refholds()

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

    def __delitem__(self, key):
        self._backend._delete_pin(str(key))


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
        return dict(node.pin_block_reasons) or None

    @property
    def exception(self):
        node = self._node()
        if node.state not in {"failed", "blocked"} or node.exception is None:
            return None
        error = node.exception
        message = str(error)
        error_type = type(error).__name__
        if isinstance(error, BaseException) and error_type != "WorkflowExecutionError" and error_type not in message:
            return f"{error_type}: {message}"
        return message

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

    def freeze(self):
        return self.context._freeze_transformer(self.node_path)

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
    def task(self):
        import asyncio

        async def run():
            return self.run()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return run()
        return loop.create_task(run())
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
