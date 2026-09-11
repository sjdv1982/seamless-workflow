"""Transport messages and controller-owned sessions."""
from dataclasses import dataclass, field
from concurrent.futures import Future
from ..sidework import Lease


class MountError(Exception): pass
class ConflictError(MountError): pass


class SyncReport(dict):
    """Detached status snapshots keyed by Context node path."""
    @property
    def errors(self):
        return {path: status['error'] or status['sense_error'] for path, status in self.items()
                if status['error'] or status['sense_error']}

    @property
    def in_sync(self):
        return all(status['in_sync'] and not status['error'] for status in self.values())


class MountLease(Lease):
    def __init__(self, checksum, role, celltype='mixed'):
        self.role = role
        super().__init__(checksum, celltype)

    def _refheld_checksums(self):
        return () if self.released or self.checksum is None else ((self.checksum, self.role),)


@dataclass(frozen=True)
class Observation:
    session_id: str
    ws: int
    fingerprint: object
    checksum: str
    buffer: object = None
    leases: tuple = ()
    reason: str = ''

    def release(self):
        for lease in self.leases: lease._release_refholds()


@dataclass(frozen=True)
class Delivery:
    session_id: str
    seq: int
    checksum: str
    celltype: str
    expected_fingerprint: object
    lease: object


@dataclass(frozen=True)
class DeliveryAck:
    session_id: str
    seq: int
    ws: int
    outcome: str
    checksum: str = None
    fingerprint: object = None
    reason: str = ''


@dataclass
class MountSession:
    session_id: str
    node_path: tuple
    spec: object
    registration: object
    celltype: str
    disk: str
    fingerprint: object
    processed_ws: int = 0
    last_synced: str = None
    pending: object = None
    in_flight: object = None
    delivery_seq: int = 0
    reasserts: tuple = ()
    sense_error: object = None
    error: object = None
    state: str = 'active'
    initial: object = field(default_factory=Future)
    retry_at: float = 0
    retry_delay: float = 1
    leaf_leases: tuple = ()
