"""Experimental callback-widget driver exercising the attachment boundary.

Widgets implement the traitlets interface: value, observe(callback, names=...),
and unobserve(callback, names=...). No widget package is imported. Widget
callbacks prepare immutable observations on their producer thread; deliveries
run in the shared daemon pool. Widget sessions are deliberately not serialized.
"""
from threading import RLock
from uuid import uuid4
from seamless import Buffer
from .api import make_sink
from .spec import AttachmentSpec
from .session import Observation, DeliveryAck, MountLease
from .policy import INVALID
from .fs.service import Registration, get_service


class WidgetDriver:
    def __init__(self, widget):
        self.widget = widget
        self.lock = RLock()
        self.writing = False
        self.registration = None

    def attach(self, cell, *, mode='rw', authority='file'):
        backend = cell._workflow_backend
        if backend is None or backend.local_path or backend.readonly:
            raise AttributeError('Widgets attach to whole Context cell nodes')
        ctx, path = backend.context, backend.node_path
        ctx._check_public_caller()
        spec = AttachmentSpec('widget-' + uuid4().hex, mode, authority, driver='widget')
        celltype = ctx._controller.call('_mount_validate', path, spec, klass=4)
        reg = self.registration = Registration(spec.path, uuid4().hex, spec, celltype, make_sink(ctx._controller))
        reg.service, reg.io_lock = self, RLock()
        self.pool = get_service()
        with self.lock:
            self.widget.observe(self._changed, names='value')
            initial = self._observation()
        try:
            ctx._controller.call('_mount_attach', path, spec, reg, initial, klass=2).result()
        except Exception:
            self.widget.unobserve(self._changed, names='value')
            raise
        finally: initial.release()
        return self

    def _observation(self):
        reg = self.registration
        reg.ws += 1
        try:
            buf = Buffer(self.widget.value, reg.celltype)
            cs = buf.get_checksum(); buf.tempref()
            return Observation(reg.session_id, reg.ws, reg.ws, cs.hex(), buf,
                               (MountLease(cs, f'widget:{reg.session_id}:observation'),))
        except Exception as exc:
            return Observation(reg.session_id, reg.ws, reg.ws, INVALID, reason=str(exc))

    def _changed(self, change):
        with self.lock:
            reg = self.registration
            if not self.writing and not reg.closed:
                reg.sink('_mount_observed', self._observation())

    def activate(self, reg):
        reg.active = True
        self.poll(reg, force=True)

    def deliver(self, reg, delivery):
        lease = MountLease(delivery.lease.checksum, f'widget:{reg.session_id}:delivery')
        def write():
            try:
                buf = self.pool._resolve(lease.checksum)
                value = buf.get_value(reg.celltype)
                with self.lock:
                    if reg.closed: raise RuntimeError('Widget session closed')
                    if reg.ws != delivery.expected_fingerprint:
                        ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'conflict')
                    else:
                        self.writing = True
                        try: self.widget.value = value
                        finally: self.writing = False
                        reg.ws += 1
                        ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'written', delivery.checksum, reg.ws)
                    reg.sink('_mount_delivered', ack)
                    if ack.outcome == 'conflict': reg.sink('_mount_observed', self._observation())
            except Exception as exc:
                reg.sink('_mount_delivered', DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'error', reason=str(exc)))
            finally: lease._release_refholds()
        return self.pool._submit(reg, write)

    def poll(self, reg, *, force=False):
        def read():
            with self.lock:
                if not reg.closed: reg.sink('_mount_observed', self._observation())
        return self.pool._submit(reg, read)

    def request_cut(self, reg, cut_id):
        def cut():
            with self.lock:
                if not reg.closed: reg.sink('_mount_observed', self._observation())
                reg.sink('_mount_cut', (reg.session_id, cut_id, reg.ws))
        return self.pool._submit(reg, cut)

    def unregister(self, reg, **kwargs):
        def close():
            with self.lock:
                reg.closed = True
                self.widget.unobserve(self._changed, names='value')
        return self.pool._submit(reg, close)
