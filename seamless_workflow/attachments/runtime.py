"""Controller-owned mount policy and finite-cut synchronization predicates."""
from dataclasses import dataclass, replace
from concurrent.futures import Future
import time
from seamless import Checksum
from .policy import ABSENT, INVALID, decide_initial, classify_observation, detector, reassert
from .session import MountSession, MountError, ConflictError, MountLease, Delivery, SyncReport
from .spec import validate_celltype
from ..errors import AuthorityError, NodeError


@dataclass
class SyncPredicate:
    cut_id: int = 0
    waiting: set = None
    activity: int = -1

    def check(self, ctx):
        if self.waiting is None:
            self.start(ctx)
            return False, None
        self.waiting.intersection_update(s.session_id for s in ctx._mount_sessions.values())
        if self.waiting: return False, None
        if any(n.state in {'waiting', 'computing'} for n in ctx._graph.nodes.values()): return False, None
        if any(s.in_flight or (s.pending and not s.error) for s in ctx._mount_sessions.values()): return False, None
        if self.activity != ctx._mount_activity:
            self.start(ctx)
            return False, None
        return True, ctx._mount_report()

    def start(self, ctx):
        ctx._mount_cut_seq += 1
        self.cut_id = ctx._mount_cut_seq
        self.activity = ctx._mount_activity
        self.waiting = {s.session_id for s in ctx._mount_sessions.values()}
        for session in ctx._mount_sessions.values():
            ctx._effects.append(lambda s=session, cut=self.cut_id: s.registration.service.request_cut(s.registration, cut))


class AttachmentRuntime:
    def _mount_register_log(self, log): self._mount_logs.add(log)
    def _mount_unregister_log(self, log): self._mount_logs.discard(log)

    def _mount_registrations(self):
        return tuple(s.registration for s in self._mount_sessions.values())

    def _mount_validate(self, path, spec):
        node = self._graph.nodes.get(path)
        if node is None or node.kind != 'cell': raise NodeError('Mounts require an existing whole cell node')
        if node.mount is not None: raise ValueError('Cell is already mounted; unmount first')
        incoming = self._incoming_for(path)
        if 'r' in spec.mode and incoming: raise AuthorityError('Sensing mount cannot have incoming edges; unmount first')
        celltype = node.cell_config.target_celltype if () in incoming else node.cell_config.celltype
        validate_celltype(celltype)
        return celltype

    def _mount_event(self, session, kind, **detail):
        event = (session.session_id, session.node_path, kind, tuple(sorted(detail.items())))
        self._mount_events.append(event)
        for log in self._mount_logs: log._append(event)

    def _mount_attach(self, path, spec, registration, observation):
        try:
            celltype = self._mount_validate(path, spec)
            if celltype != registration.celltype: raise ValueError('Celltype changed during mount preparation')
            node = self._graph.nodes[path]
            checksum = node.current_checksum.hex() if node.state == 'complete' and node.current_checksum else None
            session = MountSession(registration.session_id, path, spec, registration, celltype,
                                   observation.checksum, observation.fingerprint,
                                   processed_ws=observation.ws, last_synced=checksum)
            node.mount = spec
            self._mount_sessions[path] = session
            action = decide_initial(spec, observation.checksum, checksum)
            if checksum is not None and observation.checksum not in {ABSENT, INVALID, checksum} and spec.mode == 'rw':
                import logging
                logging.getLogger(__name__).warning('Mount %s: initial %s authority replaces differing content', spec.path, spec.authority)
            if action == 'sense': self._mount_sense(session, observation)
            elif action == 'error': self._mount_sense_error(session, observation.reason or 'Required file is missing')
            elif action == 'write': self._mount_request(session, checksum)
            if checksum == observation.checksum: self._mount_hold_leaves(session, observation)
            if action != 'write': session.initial.set_result(None)
            self._effects.append(lambda: registration.service.activate(registration))
            self._mount_event(session, 'attach', action=action)
            self._derive_all()
            return session.initial
        finally: observation.release()

    def _mount_sense_error(self, session, reason):
        if session.sense_error is None:
            import logging
            logging.getLogger(__name__).warning('Mount %s: %s', session.spec.path, reason)
        session.sense_error = MountError(f'{session.spec.path}: {reason}')

    def _mount_hold_leaves(self, session, observation):
        if session.celltype not in {'folder', 'deepfolder'}: return
        leases = tuple(MountLease(lease.checksum, f'mount:{session.session_id}:node-leaf') for lease in observation.leases[1:])
        for old in self._mount_node_leaves.get(session.node_path, ()): old._release_refholds()
        self._mount_node_leaves[session.node_path] = leases

    def _mount_sense(self, session, observation):
        self._validate_write(session.node_path, (), detach=False)
        self._set_cell_root_with_edges(session.node_path, Checksum(observation.checksum), session.celltype, clear_edges=False)
        self._mount_hold_leaves(session, observation)
        session.last_synced = observation.checksum
        # A newer sensed value supersedes an undispatched user edit.
        if session.pending:
            session.pending.lease._release_refholds()
            session.pending = None
        self._mount_activity += 1

    def _mount_observed(self, observation):
        session = None
        try:
            session = next((s for s in self._mount_sessions.values() if s.session_id == observation.session_id), None)
            if session is None: return
            kind = classify_observation(observation.ws, session.processed_ws, observation.checksum,
                                        session.disk, session.in_flight.checksum if session.in_flight else None,
                                        fingerprint=observation.fingerprint, disk_fingerprint=session.fingerprint)
            self._mount_event(session, 'observation', classification=kind, ws=observation.ws)
            if kind == 'stale': return
            session.processed_ws = observation.ws
            session.disk, session.fingerprint = observation.checksum, observation.fingerprint
            if kind == 'foreign' and 'r' in session.spec.mode:
                self._mount_sense(session, observation)
            elif kind == 'rejected' and 'r' in session.spec.mode:
                self._mount_sense_error(session, observation.reason)
            elif kind == 'absent' and session.spec.authority == 'file-strict':
                self._mount_sense_error(session, 'Required file is missing')
            elif kind == 'unchanged' and session.sense_error and observation.checksum not in {ABSENT, INVALID}:
                self._mount_sense(session, observation)
            node = self._graph.nodes[session.node_path]
            if node.current_checksum and node.current_checksum.hex() == observation.checksum:
                self._mount_hold_leaves(session, observation)
            if reassert(session.spec.mode, node.state, kind) and session.state == 'active':
                session.reasserts, tripped = detector(session.reasserts, time.monotonic())
                self._mount_event(session, 'reassert', tripped=tripped)
                if tripped:
                    session.state = 'tripped'
                    session.error = ConflictError(f'{session.spec.path}: three reasserts in 20 seconds ({observation.checksum}, {node.current_checksum.hex()})')
                    if session.pending: session.pending.lease._release_refholds(); session.pending = None
                else: self._mount_request(session, node.current_checksum.hex())
            self._derive_all()
        except Exception as exc:
            if session is not None: session.error = MountError(str(exc))
        finally: observation.release()

    def _mount_request(self, session, checksum):
        if session.state != 'active' or checksum is None: return
        session.delivery_seq += 1
        if session.pending: session.pending.lease._release_refholds()
        lease = MountLease(Checksum(checksum), f'mount:{session.session_id}:delivery:{session.delivery_seq}', session.celltype)
        session.pending = Delivery(session.session_id, session.delivery_seq, checksum, session.celltype, session.fingerprint, lease)
        session.last_synced = checksum
        session.retry_at = 0
        self._mount_activity += 1
        self._mount_event(session, 'request', checksum=checksum, seq=session.delivery_seq)

    def _mount_dispatch(self, session):
        if session.in_flight or not session.pending or session.state != 'active': return
        if session.retry_at > time.monotonic(): return
        delivery, session.pending = session.pending, None
        if delivery.checksum == session.disk:
            delivery.lease._release_refholds()
            if not session.initial.done(): session.initial.set_result(None)
            return
        delivery = replace(delivery, expected_fingerprint=session.fingerprint)
        session.in_flight = delivery
        self._mount_event(session, 'dispatch', seq=delivery.seq)
        self._effects.append(lambda: session.registration.service.deliver(session.registration, delivery))

    def _mount_after_turn(self):
        for session in tuple(self._mount_sessions.values()):
            node = self._graph.nodes.get(session.node_path)
            if node is None or node.kind != 'cell':
                self._mount_detach(session.node_path)
                continue
            if 'w' in session.spec.mode and node.state == 'complete' and node.current_checksum is not None:
                checksum = node.current_checksum.hex()
                if checksum != session.last_synced:
                    session.last_synced = checksum
                    if checksum != session.disk: self._mount_request(session, checksum)
            self._mount_dispatch(session)

    def _mount_delivered(self, ack):
        from .session import DeliveryAck
        if not isinstance(ack, DeliveryAck) or not isinstance(ack.ws, int) or ack.outcome not in {"written", "error", "conflict"}: return
        session = next((s for s in self._mount_sessions.values() if s.session_id == ack.session_id), None)
        if session is None or session.in_flight is None or session.in_flight.seq != ack.seq: return
        delivery, session.in_flight = session.in_flight, None
        self._mount_event(session, 'ack', outcome=ack.outcome, seq=ack.seq)
        if ack.outcome == 'written':
            if ack.ws > session.processed_ws:
                session.disk, session.fingerprint = ack.checksum, ack.fingerprint
                session.processed_ws = ack.ws
            if session.state != 'tripped': session.error = None
            session.retry_delay = 1
            delivery.lease._release_refholds()
        elif ack.outcome == 'error':
            if session.state != 'tripped':
                if session.error is None:
                    import logging
                    logging.getLogger(__name__).warning('Mount delivery %s: %s', session.spec.path, ack.reason)
                session.error = MountError(f'{session.spec.path}: {ack.reason}')
            if session.pending is None and session.state == 'active':
                session.pending = delivery
                session.retry_at = time.monotonic() + session.retry_delay
                session.retry_delay = min(60, session.retry_delay * 2)
            else: delivery.lease._release_refholds()
        else: delivery.lease._release_refholds()
        if not session.initial.done(): session.initial.set_result(None)
        if not self._closing: self._mount_dispatch(session)
        else:
            # Flush only already-requested pending deliveries, never retries.
            if session.spec.persistent and session.pending and not session.error:
                self._mount_dispatch(session)
            effects, self._effects = self._effects, []
            for effect in effects: effect()
            if self._mount_close_future is not None and not self._mount_close_future.done() and not self._mount_close_busy():
                self._mount_close_future.set_result(None)

    def _mount_tick(self, session_id):
        # The normal post-turn pass dispatches retries when their backoff expires.
        pass

    def _mount_cut(self, payload):
        if not isinstance(payload, tuple) or len(payload) != 3: return
        session_id, cut_id, ws = payload
        if not isinstance(session_id, str) or not isinstance(cut_id, int): return
        for predicate in self._barriers.values():
            if isinstance(predicate, SyncPredicate) and predicate.cut_id == cut_id:
                predicate.waiting.discard(session_id)

    def _mount_sync(self):
        future = Future()
        self._barriers[future] = SyncPredicate()
        return future

    def _mount_report(self):
        return SyncReport({path: self._mount_status(path) for path in self._mount_sessions})

    def _mount_status(self, path):
        session = self._mount_sessions.get(path)
        if session is None: return None
        node = self._graph.nodes[path]
        checksum = node.current_checksum.hex() if node.state == 'complete' and node.current_checksum else None
        return dict(state=session.state, node_checksum=checksum, disk_checksum=session.disk,
                    in_sync=checksum is not None and checksum == session.disk and session.sense_error is None,
                    pending=session.pending is not None, in_flight=session.in_flight is not None,
                    sense_error=session.sense_error, error=session.error)

    def _mount_clear_error(self, path):
        session = self._mount_sessions[path]
        session.error, session.state, session.reasserts = None, 'active', ()
        session.retry_at = 0
        node = self._graph.nodes[path]
        if 'w' in session.spec.mode and node.state == 'complete' and node.current_checksum:
            self._mount_request(session, node.current_checksum.hex())

    def _mount_detach(self, path, *, delete=True, derive=True):
        session = self._mount_sessions.pop(path, None)
        if session is None: return None
        session.state = 'closing'
        if session.pending: session.pending.lease._release_refholds(); session.pending = None
        # An in-flight operation retains a transport read claim until it finishes.
        if session.in_flight: session.in_flight.lease._release_refholds(); session.in_flight = None
        for lease in session.leaf_leases: lease._release_refholds()
        if not session.initial.done(): session.initial.set_result(None)
        node = self._graph.nodes.get(path)
        if node is not None: node.mount = None
        future = session.registration.service.unregister(session.registration,
                    delete=delete and not session.spec.persistent, expected=session.fingerprint)
        if derive: self._derive_all()
        return future

    def _mount_close_prepare(self):
        for session in self._mount_sessions.values():
            session.registration.active = False
            if session.spec.persistent and not session.error: self._mount_dispatch(session)
        effects, self._effects = self._effects, []
        for effect in effects: effect()

    def _mount_close_wait(self):
        if self._mount_close_future is None: self._mount_close_future = Future()
        if not self._mount_close_busy() and not self._mount_close_future.done():
            self._mount_close_future.set_result(None)
        return self._mount_close_future

    def _mount_close_busy(self):
        return any(s.in_flight or (s.pending and not s.error and s.spec.persistent) for s in self._mount_sessions.values())

    def _mount_close_finish(self):
        return [self._mount_detach(path, derive=False) for path in tuple(self._mount_sessions)]
