"""Controller-owned mount policy and finite-cut synchronization predicates."""
from dataclasses import dataclass, replace
from concurrent.futures import Future
import time
from threading import Timer
from seamless import Checksum
from .policy import ABSENT, INVALID, decide_initial, classify_observation, detector, reassert
from .session import MountSession, MountError, ConflictError, MountLease, Delivery, SyncReport
from .spec import validate_celltype
from ..errors import AuthorityError, NodeError

DELIVERY_INTERVAL = 2 / 3


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
        if any(
            s.in_flight
            or (s.pending and (s.error is None or s.retry_at == 0))
            for s in ctx._mount_sessions.values()
        ):
            return False, None
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

    def _mount_sessions_for(self, path):
        path = tuple(path)
        return tuple(
            session for (node_path, _), session in self._mount_sessions.items()
            if node_path == path
        )

    def _mount_has_sensing(self, path):
        node = self._graph.nodes.get(tuple(path))
        if node is None:
            return False
        if node.mount is not None and 'r' in node.mount.mode:
            return True
        if any('r' in spec.mode for spec in node.attachments.values()):
            return True
        return any('r' in session.spec.mode for session in self._mount_sessions_for(path))

    def _mount_has_attachments(self, path):
        node = self._graph.nodes.get(tuple(path))
        return bool(
            (node is not None and (node.mount is not None or node.attachments))
            or self._mount_sessions_for(path)
        )

    def _mount_registrations(self):
        return tuple(s.registration for s in self._mount_sessions.values())

    def _mount_transport(self, path, driver='widget'):
        session = self._mount_sessions.get((tuple(path), driver))
        return None if session is None else session.registration.service

    def _mount_validate(self, path, spec):
        path = tuple(path)
        node = self._graph.nodes.get(path)
        if node is None or node.kind != 'cell': raise NodeError('Mounts require an existing whole cell node')
        if spec.driver == 'file':
            occupied = node.mount is not None
        else:
            occupied = spec.driver in node.attachments
        if occupied or (path, spec.driver) in self._mount_sessions:
            raise ValueError('Cell is already mounted; unmount first')
        incoming = self._incoming_for(path)
        if 'r' in spec.mode and incoming: raise AuthorityError('Sensing mount cannot have incoming edges; unmount first')
        celltype = node.cell_config.celltype
        validate_celltype(celltype, spec.mode)
        return celltype

    def _mount_event(self, session, kind, **detail):
        detail.setdefault('driver', session.spec.driver)
        event = (session.session_id, session.node_path, kind, tuple(sorted(detail.items())))
        self._mount_events.append(event)
        for log in self._mount_logs: log._append(event)

    def _mount_attach(self, path, spec, registration, observation):
        try:
            celltype = self._mount_validate(path, spec)
            if celltype != registration.celltype: raise ValueError('Celltype changed during mount preparation')
            node = self._graph.nodes[path]
            if spec.driver == 'file':
                node.mount_inactive = False
            checksum = node.current_checksum.hex() if node.state == 'complete' and node.current_checksum else None
            session = MountSession(registration.session_id, path, spec, registration, celltype,
                                   observation.checksum, observation.fingerprint,
                                   processed_ws=observation.ws, last_synced=checksum)
            if spec.driver == 'file':
                node.mount = spec
            else:
                node.attachments[spec.driver] = spec
            self._mount_sessions[(path, spec.driver)] = session
            from seamless.checksum.null import NULL_CHECKSUM
            null_checksum = Checksum(NULL_CHECKSUM).hex()
            action = decide_initial(spec, observation.checksum, checksum,
                                    no_value=observation.no_value,
                                    node_is_null=checksum == null_checksum)
            if checksum is not None and observation.checksum not in {ABSENT, INVALID, checksum} and spec.mode == 'rw':
                import logging
                logging.getLogger(__name__).warning('Mount %s: initial %s authority replaces differing content', spec.path, spec.authority)
            if action == 'sense': self._mount_sense(session, observation)
            elif action == 'sense-null': self._mount_sense_null(session)
            elif action == 'sense-null-error':
                self._mount_sense_null(session)
                self._mount_sense_error(session, 'Required file is missing')
            elif action == 'error': self._mount_sense_error(session, observation.reason or 'Required file is missing')
            elif action == 'write': self._mount_request(session, checksum)
            if checksum == observation.checksum: self._mount_hold_leaves(session, observation)
            if spec.mode == 'rw' and observation.needs_canonical_write and action in {'sense', 'nothing'}:
                self._mount_request(session, observation.checksum if action == 'sense' else checksum, force=True)
            if action != 'write' and session.pending is None: session.initial.set_result(None)
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

    def _mount_sense_null(self, session):
        from seamless.checksum.null import NULL_CHECKSUM
        checksum = Checksum(NULL_CHECKSUM)
        self._validate_write(session.node_path, (), detach=False)
        self._set_cell_root_with_edges(session.node_path, checksum, session.celltype, clear_edges=False)
        session.last_synced = checksum.hex()
        self._mount_activity += 1

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
            elif kind == 'absent':
                session.sense_error = None
            elif (
                kind == 'unchanged'
                and 'r' in session.spec.mode
                and session.sense_error is not None
                and observation.checksum not in {ABSENT, INVALID}
            ):
                self._mount_sense(session, observation)
            node = self._graph.nodes[session.node_path]
            if session.spec.mode == 'rw' and observation.needs_canonical_write and kind in {'foreign', 'unchanged'} and session.sense_error is None:
                source = observation.checksum if kind == 'foreign' or node.state != 'complete' else node.current_checksum.hex()
                self._mount_request(session, source, force=True)
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

    def _mount_request(self, session, checksum, *, force=False):
        if session.state != 'active' or checksum is None: return
        session.delivery_seq += 1
        if session.pending: session.pending.lease._release_refholds()
        lease = MountLease(Checksum(checksum), f'mount:{session.session_id}:delivery:{session.delivery_seq}', session.celltype)
        session.pending = Delivery(session.session_id, session.delivery_seq, checksum, session.celltype, session.fingerprint, lease, force=force)
        session.last_synced = checksum
        session.retry_at = 0
        self._mount_activity += 1
        self._mount_event(session, 'request', checksum=checksum, seq=session.delivery_seq)

    def _mount_dispatch(self, session):
        if session.in_flight or not session.pending or session.state != 'active': return
        now = time.monotonic()
        if session.retry_at > now: return
        delivery = session.pending
        from seamless.checksum.null import NULL_CHECKSUM
        null_checksum = Checksum(NULL_CHECKSUM).hex()
        equivalent = delivery.checksum == session.disk or (
            delivery.checksum == null_checksum and session.disk == ABSENT)
        if getattr(session.registration, 'directory', False) and delivery.checksum == null_checksum:
            equivalent = True
        if equivalent and not delivery.force:
            session.pending = None
            delivery.lease._release_refholds()
            if not session.initial.done(): session.initial.set_result(None)
            return
        if now < session.last_delivery_at + DELIVERY_INTERVAL: return
        delivery, session.pending = session.pending, None
        delivery = replace(delivery, expected_fingerprint=session.fingerprint)
        session.in_flight = delivery
        session.last_delivery_at = now
        self._mount_event(session, 'dispatch', seq=delivery.seq)
        self._effects.append(lambda: session.registration.service.deliver(session.registration, delivery))

    def _mount_after_turn(self):
        for (node_path, driver), session in tuple(self._mount_sessions.items()):
            node = self._graph.nodes.get(session.node_path)
            if node is None or node.kind != 'cell':
                self._mount_detach(session.node_path, driver=driver)
                continue
            if 'w' in session.spec.mode and node.state == 'complete' and node.current_checksum is not None:
                checksum = node.current_checksum.hex()
                if checksum != session.last_synced:
                    session.last_synced = checksum
                    if checksum != session.disk: self._mount_request(session, checksum)
            self._mount_dispatch(session)
        self._mount_schedule_wakeup()

    def _mount_schedule_wakeup(self):
        now = time.monotonic()
        deadlines = [
            max(session.retry_at, session.last_delivery_at + DELIVERY_INTERVAL)
            for session in self._mount_sessions.values()
            if session.pending is not None and session.state == 'active'
            and not session.in_flight
            and (not self._closing or (
                session.spec.persistent and session.error is None
            ))
        ]
        deadline = min(deadlines) if deadlines else None
        current = self._mount_wake_deadline
        timer = self._mount_wake_timer
        if deadline is None:
            if timer is not None:
                timer.cancel()
            self._mount_wake_timer = None
            self._mount_wake_deadline = None
            return
        if timer is not None and timer.is_alive() and current is not None and current <= deadline:
            return
        if timer is not None:
            timer.cancel()
        controller = self._controller
        timer = Timer(
            max(0, deadline - now),
            controller.notify,
            args=('_mount_tick', (None,)),
            kwargs={'klass': 5, 'internal': True},
        )
        timer.daemon = True
        self._mount_wake_timer = timer
        self._mount_wake_deadline = deadline
        timer.start()

    def _mount_wakeup(self):
        self._controller.notify('_mount_tick', (None,), klass=5, internal=True)

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
                session.written_pair = (delivery.checksum, ack.checksum)
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
            self._mount_schedule_wakeup()
            if self._mount_close_future is not None and not self._mount_close_future.done() and not self._mount_close_busy():
                self._mount_close_future.set_result(None)

    def _mount_tick(self, session_id=None):
        # Both the transport poll and the Context-owned deadline timer wake the
        # ordinary post-turn dispatcher. Clear the expired timer before it
        # decides whether another wakeup is needed.
        timer = self._mount_wake_timer
        if timer is not None:
            timer.cancel()
        self._mount_wake_timer = None
        self._mount_wake_deadline = None
        if self._closing:
            for session in self._mount_sessions.values():
                if session.spec.persistent and session.pending and not session.error:
                    self._mount_dispatch(session)
            effects, self._effects = self._effects, []
            for effect in effects:
                effect()
            self._mount_schedule_wakeup()
            if (
                self._mount_close_future is not None
                and not self._mount_close_future.done()
                and not self._mount_close_busy()
            ):
                self._mount_close_future.set_result(None)

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
        return SyncReport({
            key: self._mount_status(*key)
            for key in self._mount_sessions
        })

    def _mount_status(self, path, driver='file', registration=None):
        if registration is not None:
            driver = registration.spec.driver
        path = tuple(path)
        session = self._mount_sessions.get((path, driver))
        if registration is not None and (
            session is None or session.registration is not registration
        ):
            return None
        if session is None:
            if driver != 'file':
                return None
            node = self._graph.nodes[path]
            return {"state": "inactive"} if node.mount_inactive else None
        node = self._graph.nodes[path]
        checksum = node.current_checksum.hex() if node.state == 'complete' and node.current_checksum else None
        from seamless.checksum.null import NULL_CHECKSUM
        equivalent = checksum == session.disk or session.written_pair == (checksum, session.disk) or (
            checksum == Checksum(NULL_CHECKSUM).hex() and session.disk == ABSENT)
        return dict(state=session.state, node_checksum=checksum, disk_checksum=session.disk,
                    in_sync=checksum is not None and equivalent and session.sense_error is None,
                    pending=session.pending is not None, in_flight=session.in_flight is not None,
                    sense_error=session.sense_error, error=session.error)

    def _mount_clear_error(self, path, driver='file', registration=None):
        if registration is not None:
            driver = registration.spec.driver
        path = tuple(path)
        key = (path, driver)
        if registration is None:
            session = self._mount_sessions[key]
        else:
            session = self._mount_sessions.get(key)
            if session is None or session.registration is not registration:
                return None
        session.error, session.state, session.reasserts = None, 'active', ()
        session.retry_at = 0
        node = self._graph.nodes[path]
        if 'w' in session.spec.mode and node.state == 'complete' and node.current_checksum:
            self._mount_request(session, node.current_checksum.hex())

    def _mount_detach(self, path, driver='file', registration=None, *, delete=True, derive=True):
        path = tuple(path)
        if registration is not None:
            driver = registration.spec.driver
        key = (path, driver)
        session = self._mount_sessions.get(key)
        if session is not None and registration is not None and session.registration is not registration:
            return None
        if session is None: return None
        self._mount_sessions.pop(key, None)
        session.state = 'closing'
        if session.pending: session.pending.lease._release_refholds(); session.pending = None
        # An in-flight operation retains a transport read claim until it finishes.
        if session.in_flight: session.in_flight.lease._release_refholds(); session.in_flight = None
        for lease in session.leaf_leases: lease._release_refholds()
        if not session.initial.done(): session.initial.set_result(None)
        node = self._graph.nodes.get(path)
        if node is not None:
            if driver == 'file':
                node.mount = None
                node.mount_inactive = False
            else:
                node.attachments.pop(driver, None)
        future = session.registration.service.unregister(session.registration,
                    delete=delete and not session.spec.persistent, expected=session.fingerprint)
        if derive: self._derive_all()
        return future

    def _mount_detach_all(self, path, *, delete=True, derive=True):
        path = tuple(path)
        futures = []
        for (node_path, driver) in tuple(self._mount_sessions):
            if node_path != path:
                continue
            future = self._mount_detach(
                path, driver=driver, delete=delete, derive=False
            )
            if future is not None:
                futures.append(future)
        if derive:
            self._derive_all()
        return futures

    def _mount_close_prepare(self):
        timer = self._mount_wake_timer
        if timer is not None:
            timer.cancel()
        self._mount_wake_timer = None
        self._mount_wake_deadline = None
        for session in self._mount_sessions.values():
            session.registration.active = False
            if session.spec.persistent and not session.error: self._mount_dispatch(session)
        effects, self._effects = self._effects, []
        for effect in effects: effect()
        self._mount_schedule_wakeup()

    def _mount_close_wait(self):
        if self._mount_close_future is None: self._mount_close_future = Future()
        if not self._mount_close_busy() and not self._mount_close_future.done():
            self._mount_close_future.set_result(None)
        return self._mount_close_future

    def _mount_close_busy(self):
        return any(s.in_flight or (s.pending and not s.error and s.spec.persistent) for s in self._mount_sessions.values())

    def _mount_close_finish(self):
        futures = []
        for path, driver in tuple(self._mount_sessions):
            future = self._mount_detach(path, driver=driver, derive=False)
            if future is not None:
                futures.append(future)
        return futures
