"""Experimental callback-widget driver exercising the attachment boundary.

Widgets implement the traitlets interface: value, observe(callback, names=...),
and unobserve(callback, names=...). No widget package is imported. Widget
callbacks prepare immutable observations on their producer thread; deliveries
run in the shared daemon pool. Widget sessions are deliberately not serialized.
"""
import logging
from threading import RLock, Timer, current_thread
from uuid import uuid4
from seamless import Buffer
from .api import make_sink
from .spec import AttachmentSpec
from .session import Observation, DeliveryAck, MountLease
from .policy import ABSENT, INVALID
from .fs.service import Registration, get_service


class WidgetDriver:
    delivery_timeout = 60.

    def __init__(self, widget, *, debounce=0.1):
        self.widget = widget
        self.debounce = float(debounce)
        if self.debounce < 0:
            raise ValueError("debounce must be nonnegative")
        self.lock = RLock()
        self.writing = False
        self._changed_while_writing = False
        self._timer = None
        self._pending_change = False
        self._edit_generation = 0
        self._fingerprint = 0
        self.registration = None

    def attach(self, cell, *, mode='rw', authority='file'):
        backend = cell._workflow_backend
        if backend is None or getattr(backend, 'local_path', ()) or getattr(backend, 'readonly', False):
            raise AttributeError('Widgets attach to whole Context cell nodes')
        ctx, path = backend.context, backend.node_path
        ctx._check_public_caller()
        spec = AttachmentSpec('widget-' + uuid4().hex, mode, authority, driver='widget')
        celltype = ctx._controller.call('_mount_validate', path, spec, klass=4)
        reg = self.registration = Registration(spec.path, uuid4().hex, spec, celltype, make_sink(ctx._controller))
        reg.service, reg.io_lock = self, RLock()
        self.pool = get_service()
        with self.lock:
            self._fingerprint = 0
            observe = getattr(self.widget, '_seamless_observe', None)
            if observe is None:
                self.widget.observe(self._changed, names='value')
            else:
                observe(self._changed, names='value')
            initial = self._observation()
        try:
            ctx._controller.call('_mount_attach', path, spec, reg, initial, klass=2).result()
        except Exception:
            with self.lock:
                try:
                    self.widget.unobserve(self._changed, names='value')
                except Exception:
                    pass
                if self.registration is reg:
                    self.registration = None
            raise
        finally:
            initial.release()
        return self

    def _observation(self):
        reg = self.registration
        reg.ws += 1
        try:
            value = self.widget.value
            if value is None:
                return Observation(reg.session_id, reg.ws, None, ABSENT, no_value=True)
            buf = Buffer(value, reg.celltype)
            cs = buf.get_checksum()
            buf.tempref()
            return Observation(reg.session_id, reg.ws, self._fingerprint, cs.hex(), buf,
                               (MountLease(cs, f'widget:{reg.session_id}:observation'),))
        except Exception as exc:
            return Observation(reg.session_id, reg.ws, self._fingerprint, INVALID,
                               reason=f'{type(exc).__name__}: {exc}')

    def _cancel_timer_locked(self):
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        return timer is not None

    def _changed(self, change):
        with self.lock:
            reg = self.registration
            if reg is None or reg.closed:
                return
            if self.writing:
                self._changed_while_writing = True
                return
            self._fingerprint += 1
            self._edit_generation += 1
            self._pending_change = True
            self._cancel_timer_locked()
            timer = Timer(self.debounce, self._debounced_poll, args=(reg, self._edit_generation))
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _debounced_poll(self, reg, generation):
        with self.lock:
            if self._timer is not current_thread():
                return
            self._timer = None
            if reg.closed or generation != self._edit_generation or not self._pending_change:
                return
        self.poll(reg, _generation=generation)

    def activate(self, reg):
        reg.active = True

    def deliver(self, reg, delivery):
        lease = MountLease(delivery.lease.checksum, f'widget:{reg.session_id}:delivery')

        def write():
            try:
                buf = self.pool._resolve(lease.checksum)
                value = buf.get_value(reg.celltype)
                with self.lock:
                    if reg.closed:
                        raise RuntimeError('Widget session closed')
                    flushed = self._pending_change
                    if flushed:
                        self._cancel_timer_locked()
                        self._pending_change = False
                        reg.sink('_mount_observed', self._observation())
                    changed = flushed or (
                        delivery.expected_fingerprint is not None
                        and self._fingerprint != delivery.expected_fingerprint
                    )
                    if changed:
                        ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'conflict')
                    else:
                        self.writing = True
                        self._changed_while_writing = False
                        try:
                            self.widget.value = value
                        finally:
                            self.writing = False
                        changed_while_writing = self._changed_while_writing
                        if changed_while_writing:
                            self._fingerprint += 1
                        coerced = (changed_while_writing
                                   and not self._same_value(self.widget.value, value))
                        reg.ws += 1
                        ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'written',
                                          delivery.checksum, self._fingerprint)
                    reg.sink('_mount_delivered', ack)
                    coerced = ack.outcome == 'written' and coerced and 'r' in reg.spec.mode
                    self._changed_while_writing = False
                    if ack.outcome == 'conflict' and not flushed:
                        reg.sink('_mount_observed', self._observation())
                    elif coerced and not reg.closed:
                        reg.sink('_mount_observed', self._observation())
            except Exception as exc:
                reg.sink('_mount_delivered',
                         DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'error',
                                     reason=f'{type(exc).__name__}: {exc}'))
            finally:
                lease._release_refholds()

        return self.pool._submit(reg, write)

    @staticmethod
    def _same_value(left, right):
        if left is right:
            return True
        try:
            return bool(left == right)
        except Exception:
            return False

    def poll(self, reg, *, force=False, _generation=None):
        if _generation is None:
            with self.lock:
                self._cancel_timer_locked()

        def read():
            with self.lock:
                if reg.closed:
                    return
                if _generation is not None:
                    if (_generation != self._edit_generation or not self._pending_change):
                        return
                    self._pending_change = False
                else:
                    self._pending_change = False
                reg.sink('_mount_observed', self._observation())

        return self.pool._submit(reg, read)

    def request_cut(self, reg, cut_id):
        def cut():
            with self.lock:
                self._cancel_timer_locked()
                if not reg.closed:
                    self._pending_change = False
                    reg.sink('_mount_observed', self._observation())
                    reg.sink('_mount_cut', (reg.session_id, cut_id, reg.ws))
        return self.pool._submit(reg, cut)

    def unregister(self, reg, **kwargs):
        detached = getattr(self.widget, '_seamless_detached', None)
        if detached is not None:
            detached()

        def close():
            with self.lock:
                self._cancel_timer_locked()
                self._pending_change = False
                reg.closed = True
                try:
                    unobserve = getattr(self.widget, '_seamless_unobserve', None)
                    if unobserve is None:
                        self.widget.unobserve(self._changed, names='value')
                    else:
                        unobserve(self._changed, names='value')
                except Exception:
                    pass
        return self.pool._submit(reg, close)
