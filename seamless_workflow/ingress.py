"""Producer-side public operations. No live graph state is used by these wrappers."""
from __future__ import annotations
import asyncio
import copy
from dataclasses import replace
from functools import wraps
from seamless import Buffer, Cell, Checksum
from .adapters import checksum_for_value
from .sidework import Lease, PreparedCell, PreparedTransformer
from .graph import TransformerConfig
from .errors import ConcurrentUpdateError, ValueUnavailableError, StaleWorkflowHandleError


def _endpoint(value):
    from .endpoints import BoundEndpoint
    if isinstance(value, BoundEndpoint): return value
    method = getattr(value, '_workflow_endpoint', None)
    return method() if callable(method) else None


def _prepare_transformer(ctx, value):
    from seamless_transformer import delayed
    from seamless_transformer.transformer_class import TransformerCore
    if not isinstance(value, TransformerCore):
        value = delayed(value)
    snapshot = value._snapshot_for_call()
    args = {pin: _endpoint(arg) or checksum_for_value(arg, snapshot.celltypes.get(pin, 'mixed'))
            for pin, arg in snapshot.args.items()}
    snapshot = replace(snapshot, args=args)
    cfg, snapshot = ctx._transformer_config_from_snapshot(snapshot)
    return PreparedTransformer(cfg, snapshot)


def _prepare_assignment(ctx, path, value):
    from seamless_transformer.transformer_class import TransformerCore
    ep = _endpoint(value)
    if ep is not None: return ep
    if isinstance(value, Cell):
        ref = value.input_ref
        if ref is not None:
            ref = (_prepare_assignment(ctx, path, ref) if isinstance(ref, Cell) else
                   (_endpoint(ref) or checksum_for_value(ref, value.celltype)))
        return PreparedCell(value.celltype, value.target_celltype, value.validator, value.validator_language, ref)
    if isinstance(value, TransformerCore) or callable(value):
        return _prepare_transformer(ctx, value)
    from .context import Context
    from .views import SubContextView
    if isinstance(value, (Context, SubContextView)): return value
    try:
        node = ctx._node_snapshot(path)
    except StaleWorkflowHandleError:
        celltype = 'mixed'
    else:
        if node.kind == 'transformer':
            return _prepare_transformer(ctx, value).config
        celltype = node.cell_config.celltype
    return checksum_for_value(value, celltype)


def _wait(ctx, path=None, local=(), *, read=False, barrier=False, timeout=None):
    future = ctx._controller.call('_install_wait', path, local, read=read, barrier=barrier, klass=1 if barrier else 4)
    try:
        return future.result(timeout)
    finally:
        if ctx._controller.accepting:
            ctx._controller.call('_withdraw_wait', future, klass=1)


async def _wait_async(ctx, path=None, local=(), *, read=False, barrier=False, timeout=None):
    installed = ctx._controller.submit('_install_wait', (path, local), {'read':read, 'barrier':barrier}, klass=1 if barrier else 4)
    try:
        future = await asyncio.shield(asyncio.wrap_future(installed))
    except asyncio.CancelledError:
        def withdraw(reply):
            if not reply.cancelled() and reply.exception() is None:
                ctx._controller.notify('_withdraw_wait', (reply.result(),), klass=1)
        installed.add_done_callback(withdraw)
        raise
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout)
    finally:
        if ctx._controller.accepting:
            await asyncio.wrap_future(ctx._controller.submit('_withdraw_wait', (future,), klass=1))


def _edit(ctx, path, local, value=None, *, detach=False, delete=False, operation=None, checksum_rhs=False):
    from .context import _assign_path, _delete_path, _read_path
    import operator
    for attempt in range(8):
        lease, revision, state, incoming = ctx._controller.call('_update_base', path, local, detach, klass=4)
        try:
            if lease.checksum is None:
                if state in {'waiting','computing'}:
                    _wait(ctx, path, barrier=True)
                    continue
                if local and isinstance(local[0], str) and not incoming:
                    root = {}
                else:
                    raise ValueUnavailableError(f'Value for {path!r} is unavailable')
            else:
                root = lease.checksum.resolve(lease.celltype)
            if operation:
                replacement = getattr(operator, operation)(_read_path(root, local) if local else root, value)
            elif checksum_rhs:
                replacement = Checksum(value).resolve(lease.celltype)
            else:
                replacement = value
            if not local:
                root = replacement
            elif delete:
                _delete_path(root, local)
            else:
                _assign_path(root, local, replacement)
            checksum = checksum_for_value(root, lease.celltype)
            base = lease.checksum.hex() if lease.checksum is not None else None
            output = Lease(checksum)
            try:
                if ctx._controller.call('_commit_update', path, local, checksum, base, revision, detach, klass=3):
                    return
            finally:
                output._release_refholds()
        finally:
            lease._release_refholds()
    raise ConcurrentUpdateError(f'Concurrent updates to {path!r} exceeded 8 retries')


def controller_method(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        controller = self._controller
        if controller.is_owner():
            if not method.__name__.startswith('_'):
                from .errors import ReentrantContextError
                raise ReentrantContextError('Public API called from the controller')
            return method(self, *args, **kwargs)
        name = method.__name__
        if name == '_refheld_checksums' and self._refholds_released: return ()
        if name == '_release_refholds':
            return self.close()
        original = None
        leases = []
        if name == '_assign':
            path, original = args
            value = _prepare_assignment(self, path, original)
            args = (path, value)
            if isinstance(value, Checksum): leases.append(Lease(value))
            if isinstance(value, PreparedCell) and isinstance(value.input_ref, Checksum): leases.append(Lease(value.input_ref))
            if isinstance(value, PreparedTransformer):
                leases.extend(Lease(v) for v in value.snapshot.args.values() if isinstance(v, Checksum))
        elif name in {'_replace_transformer_from_builder','_create_transformer_from_builder'}:
            path, original = args
            args = (path, _prepare_transformer(self, original))
        elif name == '_set_transformer_code':
            path, value = args
            ep = _endpoint(value)
            if ep is None:
                if value is None: value = TransformerConfig()
                else:
                    prepared = _prepare_transformer(self, value).config
                    old = self._node_snapshot(path).transformer_config
                    old.code, old.code_checksum, old.callable = prepared.code, prepared.code_checksum, prepared.callable
                    old.pins.update(prepared.pins)
                    for pin in prepared.pins: old.celltypes.setdefault(pin, 'mixed')
                    from .configuration import fingerprint
                    fingerprint(old)
                    value = old
            else: value = ep
            args = (path, value)
        elif name == '_set_transformer_pin':
            path, pin, value = args
            ep = _endpoint(value)
            if ep is None:
                cfg = self._node_snapshot(path).transformer_config
                value = checksum_for_value(value, cfg.celltypes.get(pin, 'mixed'))
                leases.append(Lease(value))
            else: value = ep
            args = (path, pin, value)
        elif name == '_cell_operation':
            path, local, value = args
            ep = _endpoint(value)
            if ep is None and local:
                return _edit(self, path, tuple(local), copy.deepcopy(value), **kwargs)
            if ep is None:
                cfg = self._node_snapshot(path).cell_config
                value = Checksum(value) if kwargs.get('checksum_rhs') else checksum_for_value(value, cfg.celltype)
                leases.append(Lease(value))
            else: value = ep
            args = (path, local, value)
        elif name == '_cell_delete_path' and args[1]:
            return _edit(self, *args, detach=True, delete=True)
        elif name in {'_get_value','_get_buffer','_get_checksum'}:
            path = args[0]; local = args[1] if len(args)>1 else ()
            lease = controller.call('_read_snapshot', path, local, klass=4)
            try:
                checksum = lease.checksum
                if checksum is not None and local:
                    from .sidework import evaluate_projection
                    try: checksum = evaluate_projection(checksum, local, lease.celltype, lease.celltype)
                    except (KeyError, IndexError): checksum = None
                if name == '_get_checksum':
                    if checksum is not None: checksum.tempref()
                    return checksum
                if checksum is None: return None
                return checksum.resolve(kwargs.get('celltype') or lease.celltype) if name == '_get_value' else checksum.resolve()
            finally:
                lease._release_refholds()
        elif name in {'_compute_cell_endpoint','_compute_cell_value','_compute_node'}:
            path = args[0]; local = args[1] if len(args)>1 else ()
            lease = _wait(self, path, local, read=True, barrier=True, timeout=kwargs.get('timeout'))
            try:
                if lease.checksum is None: return None
                if local:
                    from .sidework import evaluate_projection
                    projected = evaluate_projection(lease.checksum, local, lease.celltype, lease.celltype)
                else: projected = lease.checksum
                if name == '_compute_cell_value' or (name == '_compute_node' and not kwargs.get('checksum')):
                    return projected.resolve(lease.celltype)
                projected.tempref()
                return projected
            finally:
                lease._release_refholds()
        elif name == '_set_node_config':
            from .configuration import update_config, fingerprint
            path, field, value = args
            for _ in range(8):
                cfg, revision = controller.call('_config_snapshot', path, klass=4)
                cfg = update_config(cfg, field, value, **kwargs)
                fingerprint(cfg)
                if controller.call('_publish_config', path, revision, cfg, klass=2): return
            raise ConcurrentUpdateError('Concurrent configuration updates exceeded retry budget')
        elif name == 'set_graph':
            from .serialization import prepare_graph
            args = (prepare_graph(copy.deepcopy(args[0])),)
        try:
            reads = {'get_graph', '_node_snapshot', '_lookup', '_incoming_edge',
                     '_public_source', '_snapshot_transformer', '_build_cell_expression',
                     '_build_source_expression', '_capture_endpoint', '_refheld_checksums'}
            klass = 4 if name in reads else (1 if name in {'prune', '_clear_exception'} else 2)
            result = controller.call(name, *args, klass=klass, **kwargs)
            if name == 'get_graph':
                for entry in result['nodes']:
                    code = entry.get('code')
                    if hasattr(code, 'decode'): entry['code'] = code.decode()
            if original is not None and hasattr(original, '_workflow_backend') and _endpoint(original) is None:
                from .builder_state import BoundCellBackend, BoundTransformerBackend
                backend = BoundCellBackend if isinstance(original, Cell) else BoundTransformerBackend
                object.__setattr__(original, '_workflow_backend', backend(self, args[0]))
                original._release_refholds()
            return result
        finally:
            for lease in leases: lease._release_refholds()

    return call
