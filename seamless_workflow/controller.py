"""Sequenced, non-yielding Context ingress. No execution substrate lives here."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Event, Lock, Thread, get_ident, current_thread
import weakref

from .errors import ClosedContextError, ReentrantContextError, ControllerFailedError


@dataclass(frozen=True)
class ContextMessage:
    context_id: str
    sequence: int
    klass: int
    operation: str
    args: tuple
    kwargs: tuple
    reply: Future


class Controller:
    def __init__(self, context):
        self.context = weakref.ref(context)
        self.context_id = context.top_id
        self.lock = Lock()
        self.queue = deque()
        self.sequence = 0
        self.accepting = True
        self.stopped = False
        self.failure = None
        self.trace = deque(maxlen=1024)
        self.turn_logs = set()
        self.ready = Event()
        self.thread = Thread(target=self._run, name=f"Context-{self.context_id[:8]}", daemon=True)
        self.thread.start()
        self.ready.wait()

    def _run(self):
        self.ident = get_ident()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def is_owner(self):
        return current_thread() is self.thread

    def assert_owner(self):
        assert self.is_owner(), "Live graph access outside Context controller"

    def submit(self, operation, args=(), kwargs=None, *, klass=2):
        if self.is_owner():
            raise ReentrantContextError("Synchronous public ingress from the controller")
        return self.enqueue(operation, args, kwargs, klass=klass)

    def enqueue(self, operation, args=(), kwargs=None, *, klass=5, internal=False):
        reply = Future()
        with self.lock:
            if self.failure is not None and not internal:
                raise self.failure
            if self.stopped or (not self.accepting and not internal):
                raise ClosedContextError("Context is closed")
            self.sequence += 1
            message = ContextMessage(self.context_id, self.sequence, klass, operation, tuple(args), tuple((kwargs or {}).items()), reply)
            self.queue.append(message)
            self.loop.call_soon_threadsafe(self._drain)
        return reply

    def notify(self, operation, args=(), *, klass=5, internal=False):
        try: self.enqueue(operation, args, klass=klass, internal=internal)
        except (ClosedContextError, ControllerFailedError): pass

    def begin_close(self):
        with self.lock:
            self.accepting = False
        return self.enqueue("_begin_close", klass=1, internal=True)

    def call(self, operation, *args, klass=2, **kwargs):
        return self.submit(operation, args, kwargs, klass=klass).result()

    def _drain(self):
        self.assert_owner()
        with self.lock:
            if not self.queue:
                return
            message = self.queue.popleft()
        context = self.context()
        if context is None:
            message.reply.set_exception(ClosedContextError("Context no longer exists"))
            return
        self.trace.append((message.sequence, message.klass, message.operation, get_ident()))
        try:
            if self.failure is not None and message.operation not in {'_begin_close', '_finish_close', '_mount_close_busy', '_mount_close_wait', '_mount_close_finish', '_mount_delivered', '_mount_cut'}:
                raise self.failure
            method = getattr(context, message.operation)
            original = getattr(method, '__wrapped__', None)
            result = (original(context, *message.args, **dict(message.kwargs))
                      if original is not None else method(*message.args, **dict(message.kwargs)))
            context._after_turn()
            if self.turn_logs:
                from .diagnostics import NodeSnapshot, TurnSnapshot
                snapshot = TurnSnapshot(message.sequence, message.klass, tuple(
                    NodeSnapshot(path, node.state, node.current_checksum.hex() if node.current_checksum is not None else None)
                    for path, node in sorted(context._graph.nodes.items())))
                for log in self.turn_logs: log._append(snapshot)
        except BaseException as exc:
            if message.klass == 5:
                self.poison(context, message.operation, exc)
            try: context._after_turn()
            except BaseException: pass
            if not message.reply.done():
                message.reply.set_exception(exc)
        else:
            if not message.reply.done():
                message.reply.set_result(result)

    def poison(self, context, operation, exc):
        """Refuse all further ingress after a controller-internal failure (§12.3).

        Called on the controller thread, either by ``_drain`` for a failed
        class-5 turn or by a turn that fails after publishing graph state.
        The failing request still receives its original exception.
        """
        self.assert_owner()
        if self.failure is not None:
            return
        self.failure = ControllerFailedError(f'Controller {operation} failed: {type(exc).__name__}: {exc}')
        with self.lock:
            self.accepting = False
        for future in context._barriers:
            if not future.done(): future.set_exception(self.failure)
        context._barriers.clear()
        context._begin_close()

    def stop(self):
        with self.lock:
            self.accepting = False
            self.stopped = True
            pending = tuple(self.queue)
            self.queue.clear()
            self.turn_logs.clear()
        for message in pending:
            if not message.reply.done():
                message.reply.set_exception(ClosedContextError("Context closed"))
        self.loop.call_soon_threadsafe(self.loop.stop)
        if not self.is_owner():
            self.thread.join()
