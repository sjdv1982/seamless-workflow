"""Checksum-wired reactive continuation through the ordinary Transformation API."""
import asyncio
import copy
from dataclasses import replace
from time import time
import weakref
from seamless import Checksum
from .scheduler import RunRecord, ExceptionInfo
from .sidework import Lease
from .errors import WorkflowExecutionError


class Reactive:
    def _derive_transformer(self, path, node):
        cfg = node.transformer_config
        incoming = self._incoming_for(path)
        code = cfg.code_checksum
        if ('code',) in incoming:
            state, code = self._source_state(incoming[('code',)])
            if state != 'complete':
                self._suspend(path)
                self._apply_pending(node, [incoming[('code',)]])
                return
        if code is None:
            self._suspend(path)
            node.state, node.block_reason, node.exception = 'unwired', None, None
            self._replace_current_checksum(path, None)
            return
        pins = {}
        for pin in sorted(cfg.pins):
            edge = incoming.get((pin,))
            if edge is not None:
                state, checksum = self._source_state(edge)
                if state != 'complete':
                    self._suspend(path)
                    self._apply_pending(node, [edge])
                    return
            else:
                producer = node.transformer_pin_producers.get(pin)
                checksum = producer.checksum if producer else None
            if checksum is None:
                if pin in cfg.optional_pins:
                    continue
                self._suspend(path)
                node.state, node.block_reason, node.exception = 'unwired', None, None
                self._replace_current_checksum(path, None)
                return
            pins[pin] = checksum
        key = (code.hex(), tuple((pin, cs.hex()) for pin, cs in sorted(pins.items())), cfg.config_token)
        current = self._runtime.current_runs.get(path)
        if current is not None and current.demand_key == key:
            self._publish_run(path, current)
            return
        self._suspend(path)
        held = self._runtime.superseded_runs.get(path, ())
        for record in tuple(held):
            if record.demand_key == key and record.phase != 'cancelled':
                held.remove(record)
                if record.result_checksum is not None or record.exception:
                    # Even a previously completed held run arrives as a later
                    # fact, never as a cache lookup performed by the edit turn.
                    payload = Lease(record.result_checksum) if record.result_checksum is not None else None
                    args = (path, record.generation, record.identity_checksum, payload, record.error)
                    record.result_checksum = None
                    record.exception = record.error = None
                    controller = self._controller
                    self._effects.append(lambda args=args: controller.notify('_transformation_finished', args))
                record.phase = 'running'
                record.hold_deadline = None
                self._runtime.current_runs[path] = record
                self._publish_run(path, record)
                return
        generation = self._runtime.next_generation()
        record = RunRecord(path, None, None, None, phase='running', generation=generation, demand_key=key)
        self._runtime.current_runs[path] = record
        self._replace_current_checksum(path, None)
        node.state, node.block_reason, node.exception = 'computing', None, None
        snapshot = self._snapshot_transformer(path, concrete_args=pins, concrete_code=code)
        snapshot = replace(snapshot, signature=None)
        leases = snapshot.leases
        owner = weakref.ref(self)
        side = self._side
        async def execute(snapshot=snapshot, leases=leases):
            from seamless_transformer.transformer_class import Transformer
            tf = None
            completion = None
            def send(operation, *args):
                ctx = owner()
                if ctx is not None and ctx._controller.accepting:
                    try: ctx._controller.submit(operation, args, klass=5)
                    except RuntimeError:
                        for item in args:
                            if isinstance(item, Lease): item._release_refholds()
                else:
                    for item in args:
                        if isinstance(item, Lease): item._release_refholds()
            try:
                builder = Transformer.__new__(Transformer)
                tf = builder._build_from_snapshot(snapshot)
                tf_checksum = await tf.construction()
                if tf_checksum is None:
                    raise RuntimeError(tf.exception or 'Transformation construction failed')
                send('_transformation_started', path, generation, tf_checksum)
                from seamless_transformer.observation import observed_as
                with observed_as(".".join(path)):
                    result = await tf.computation(require_value=True)
                if result is None:
                    raise RuntimeError(tf.exception or 'Transformation failed')
                completion = (path, generation, tf_checksum, Lease(result), None)
            except asyncio.CancelledError:
                if tf is not None:
                    await tf.cancel_async()
                raise
            except Exception as exc:
                completion = (path, generation, None, None, WorkflowExecutionError(str(exc)))
            finally:
                for lease in leases: lease._release_refholds()
                if tf is not None: tf._release_refholds()
            if completion is not None:
                send('_transformation_finished', *completion)
        self._effects.append(lambda: setattr(record, 'et', side.submit(execute())))

    def _publish_run(self, path, record):
        node = self._graph.nodes[path]
        if record.exception is not None:
            node.state, node.block_reason, node.exception = 'failed', None, record.error
            self._replace_current_checksum(path, None)
        elif record.result_checksum is not None:
            self._replace_current_checksum(path, record.result_checksum)
            node.state, node.block_reason, node.exception = 'complete', None, None
        else:
            node.state, node.block_reason, node.exception = 'computing', None, None
            self._replace_current_checksum(path, None)

    def _suspend(self, path):
        current = self._runtime.current_runs.get(path)
        if current is None:
            return
        self._runtime.supersede(path)
        if current.phase == 'superseded':
            deadline = current.hold_deadline
            delay = max(0, min(300, deadline-time())) if deadline is not None else 300
            controller = self._controller
            # A timer holds the weak-owning controller, never its Context.
            self._effects.append(lambda: controller.loop.call_later(delay, controller.notify,
                '_expire_run', (path, current.generation)))
        # The cap is enforced by the ledger; release memberships of evicted runs.
        for record in self._runtime.evicted:
            if record.et is not None: self._effects.append(record.et.cancel)
        self._runtime.evicted.clear()

    def _enqueue_expiry(self, path, generation):
        # Timer callback only enqueues; it never reads the graph.
        self._controller.enqueue('_expire_run', (path, generation), klass=5)

    def _expire_run(self, path, generation):
        queue = self._runtime.superseded_runs.get(path, ())
        for record in tuple(queue):
            if record.generation == generation and record.hold_deadline is not None and record.hold_deadline <= time():
                queue.remove(record)
                record.phase = 'cancelled'
                if record.et is not None: self._effects.append(record.et.cancel)
        self._sync_superseded_refholds()

    def _find_run(self, path, generation):
        current = self._runtime.current_runs.get(path)
        if current is not None and current.generation == generation:
            return current
        for record in self._runtime.superseded_runs.get(path, ()):
            if record.generation == generation and record.phase != 'cancelled':
                return record
        return None

    def _transformation_started(self, path, generation, tf_checksum):
        record = self._find_run(path, generation)
        if record is None: return
        record.identity_checksum = tf_checksum
        self._derive_all()

    def _transformation_finished(self, path, generation, tf_checksum, lease, error):
        record = None if self._closing else self._find_run(path, generation)
        if record is None:
            if lease is not None: lease._release_refholds()
            return
        if tf_checksum is not None: record.identity_checksum = tf_checksum
        record.result_checksum = lease.checksum if lease is not None else None
        record.error = error
        record.exception = ExceptionInfo.from_exception(error) if error else None
        if record.phase != 'superseded': record.phase = 'completed'
        self._sync_superseded_refholds()
        self._derive_all()
        if lease is not None: lease._release_refholds()
