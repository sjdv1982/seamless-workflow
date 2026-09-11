"""Deterministic file-policy transport for tests; never serialized."""
from collections import deque
from concurrent.futures import Future
from types import SimpleNamespace
from uuid import uuid4
from seamless import Buffer
from .api import make_sink
from .spec import AttachmentSpec
from .session import Observation, DeliveryAck, MountLease
from .policy import ABSENT, INVALID


class ManualDriver:
    delivery_timeout = 60

    def __init__(self):
        self.deliveries = deque()
        self.cuts = deque()
        self.registration = None

    def attach(self, cell, value=None, *, mode='rw', authority='file', rejected=None):
        backend = cell._workflow_backend
        ctx, path = backend.context, backend.node_path
        spec = AttachmentSpec('manual-' + uuid4().hex, mode, authority, driver='manual')
        celltype = ctx._controller.call('_mount_validate', path, spec, klass=4)
        reg = self.registration = SimpleNamespace(session_id=uuid4().hex, celltype=celltype,
            service=self, sink=make_sink(ctx._controller), active=False, closed=False, ws=0, spec=spec)
        observation = self.observation(value, rejected=rejected)
        ctx._controller.call('_mount_attach', path, spec, reg, observation, klass=2)
        return self

    def observation(self, value=None, *, ws=None, rejected=None):
        reg = self.registration
        if ws is None: reg.ws += 1; ws = reg.ws
        if rejected is not None: return Observation(reg.session_id, ws, ws, INVALID, reason=rejected)
        if value is None: return Observation(reg.session_id, ws, ws, ABSENT)
        buf = Buffer(value, reg.celltype)
        cs = buf.get_checksum(); buf.tempref()
        return Observation(reg.session_id, ws, ws, cs.hex(), buf, (MountLease(cs, 'manual:observation'),))

    def observe(self, value=None, **kwargs):
        observation = self.observation(value, **kwargs)
        self.registration.sink('_mount_observed', observation)
        return observation

    def activate(self, reg): reg.active = True
    def deliver(self, reg, delivery): self.deliveries.append(delivery)
    def request_cut(self, reg, cut_id): self.cuts.append(cut_id)
    def poll(self, reg, *, force=False): pass

    def ack(self, delivery=None, *, outcome='written', ws=None, reason='injected write failure'):
        if delivery is None: delivery = self.deliveries.popleft()
        reg = self.registration
        if ws is None: reg.ws += 1; ws = reg.ws
        reg.sink('_mount_delivered', DeliveryAck(reg.session_id, delivery.seq, ws, outcome,
                 delivery.checksum, ws, reason))

    def cut(self):
        reg = self.registration
        reg.sink('_mount_cut', (reg.session_id, self.cuts.popleft(), reg.ws))

    def unregister(self, reg, **kwargs):
        reg.closed = True
        future = Future(); future.set_result(None)
        return future
