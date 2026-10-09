"""Caller-side mount preparation and public handles."""
import asyncio
import weakref
from concurrent.futures import Future
from uuid import uuid4
from .spec import AttachmentSpec
from .fs.service import get_service
from ..errors import ClosedContextError, ControllerFailedError


def make_sink(controller):
    reference = weakref.ref(controller)
    def sink(operation, payload):
        def fail(error):
            if hasattr(payload, 'release'):
                payload.release()
            reply = Future()
            reply.set_exception(error)
            return reply
        owner = reference()
        if owner is None:
            return fail(ClosedContextError("Context is closed"))
        try:
            reply = owner.enqueue(operation, (payload,), klass=3 if operation == '_mount_observed' else 5,
                                  internal=operation != '_mount_observed')
        except (ClosedContextError, ControllerFailedError) as exc:
            return fail(exc)
        except BaseException as exc:
            return fail(exc)
        else:
            if hasattr(payload, 'release'):
                # Also covers accepted messages discarded by stop or poison.
                reply.add_done_callback(lambda future: payload.release())
            return reply
    return sink


class MountHandle:
    def __init__(self, backend):
        self.backend = backend

    def _context(self):
        b = self.backend
        b.context._check_public_caller()
        if getattr(b, "local_path", ()) or getattr(b, "readonly", False):
            raise AttributeError('Only whole Context cell nodes can be mounted')
        from ..errors import NodeError, StaleWorkflowHandleError

        try:
            b._node()
        except StaleWorkflowHandleError as exc:
            raise NodeError('Mounts require an existing whole cell node') from exc
        return b.context, b.node_path

    def __call__(self, path, mode='rw', authority='file', *, persistent=True):
        ctx, node_path = self._context()
        spec = AttachmentSpec(path, mode, authority, persistent)
        celltype = ctx._controller.call('_mount_validate', node_path, spec, klass=4)
        service = get_service()
        registration = service.reserve(spec, celltype, uuid4().hex, make_sink(ctx._controller))
        try:
            observation = service.initial_read(registration).result()
            try:
                initial = ctx._controller.call('_mount_attach', node_path, spec, registration, observation, klass=2)
            finally: observation.release()
            initial.result()
        except Exception:
            service.unregister(registration).result()
            raise

    def unmount(self):
        ctx, path = self._context()
        future = ctx._controller.call('_mount_detach', path, klass=2)
        if future is not None: future.result(get_service().delivery_timeout)

    @property
    def spec(self):
        ctx, path = self._context()
        return ctx._node_snapshot(path).mount

    @property
    def status(self):
        ctx, path = self._context()
        return ctx._controller.call('_mount_status', path, klass=4)

    @property
    def error(self):
        status = self.status
        return None if status is None else status['error']

    def clear_error(self):
        ctx, path = self._context()
        ctx._controller.call('_mount_clear_error', path, klass=2)


class ContextMounts:
    def __init__(self, context): self.context = context

    def sync(self, timeout=None):
        ctx = self.context
        ctx._check_public_caller()
        future = ctx._controller.call('_mount_sync', klass=1)
        try: return future.result(timeout)
        finally:
            if ctx._controller.accepting: ctx._controller.call('_withdraw_wait', future, klass=1)

    async def synchronization(self, timeout=None):
        ctx = self.context
        ctx._check_public_caller()
        installed = ctx._controller.submit('_mount_sync', klass=1)
        try: future = await asyncio.shield(asyncio.wrap_future(installed))
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

    @property
    def errors(self):
        report = self.context._controller.call('_mount_report', klass=4)
        return {p: s['error'] or s['sense_error'] for p, s in report.items() if s['error'] or s['sense_error']}


def load_graph(ctx, data, *, mounts=True, shares=True):
    context_shares = ctx.shares
    with context_shares._lock:
        return _load_graph(
            ctx, data, mounts=mounts, shares=shares,
            context_shares=context_shares,
        )


def _load_graph(ctx, data, *, mounts=True, shares=True, context_shares):
    """Load a graph, optionally restoring its file mounts and HTTP shares."""
    import copy
    from .spec import validate_celltype
    from ..serialization import prepare_graph
    graph = prepare_graph(copy.deepcopy(data))
    old = ctx._controller.call('_mount_registrations', klass=4)
    old_shares = tuple(
        registration for registration in old
        if getattr(getattr(registration, 'spec', None), 'driver', None) == 'share'
    )
    prepared, reservations = [], []
    service = get_service() if mounts and any(n.mount for n in graph.nodes.values()) else None
    share_prepared = []
    share_driver = (
        context_shares._driver
        if shares and any('share' in n.attachments for n in graph.nodes.values())
        else None
    )
    try:
        reads = []
        for path, node in graph.nodes.items():
            spec, node.mount = node.mount, None
            share_spec = node.attachments.pop('share', None)
            if spec is not None and mounts:
                celltype = node.cell_config.celltype
                validate_celltype(celltype, spec.mode)
                reg = service.reserve(spec, celltype, uuid4().hex, make_sink(ctx._controller), replaces=old)
                reservations.append(reg)
                reads.append(('mount', path, spec, reg, service.initial_read(reg)))
            if share_spec is not None and shares:
                celltype = node.cell_config.celltype
                reg = share_driver.reserve(
                    share_spec, celltype, uuid4().hex, make_sink(ctx._controller),
                    replaces=old_shares, staged=True, node_path=path,
                )
                reservations.append(reg)
                reads.append(('share', path, share_spec, reg, share_driver.initial_read(reg)))
        if share_driver is not None:
            share_driver.start()
        for kind, path, spec, reg, future in reads:
            prepared_item = (path, spec, reg, future.result())
            (prepared if kind == 'mount' else share_prepared).append(prepared_item)
        initial = ctx._controller.call(
            'set_graph', graph, mount_prepared=tuple(prepared),
            share_prepared=tuple(share_prepared), klass=2,
        )
        for future in initial: future.result()
    except BaseException:
        for reg in reversed(reservations):
            try:
                future = reg.service.unregister(reg)
                if future is not None:
                    future.result(getattr(reg.service, 'delivery_timeout', 60))
            except BaseException:
                pass
        raise
    finally:
        for _, _, _, observation in (*prepared, *share_prepared): observation.release()
