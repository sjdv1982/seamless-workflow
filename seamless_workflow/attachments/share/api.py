"""Public share handles bound to whole Context cells."""

from concurrent.futures import Future
import weakref
from threading import RLock
from uuid import uuid4

from .driver import ShareDriver
from .spec import ShareSpec
from ..api import make_sink
from ...errors import NodeError, StaleWorkflowHandleError


class ContextShares:
    def __init__(self, context):
        self._context_ref = weakref.ref(context)
        self._lock = RLock()
        self._namespace = "ctx"
        self._owner = object()
        self._driver = ShareDriver(self._namespace, self._owner)

    def _context(self):
        context = self._context_ref()
        if context is None:
            from ...errors import ClosedContextError
            raise ClosedContextError("Context is closed")
        if context._closing or context._closed_event.is_set():
            from ...errors import ClosedContextError
            raise ClosedContextError("Context is closed")
        context._check_public_caller()
        return context

    @property
    def namespace(self):
        self._context()
        return self._namespace

    @namespace.setter
    def namespace(self, value):
        context = self._context()
        with self._lock:
            context = self._context()
            if not isinstance(value, str) or not value or any(
                char in value for char in "/\\?#\x00"
            ) or value in {".", ".."} or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("namespace must be one safe URL segment")
            if value == self._namespace:
                return
            if context._controller.call("_share_has_sessions", klass=4):
                raise ValueError("cannot change share namespace while shares are active")
            future = self._driver.release_namespace()
            future.result(self._driver.delivery_timeout)
            self._namespace = value
            self._driver.namespace = value

    @property
    def url(self):
        self._context()
        base = self._driver.url
        return None if base is None else base.rstrip("/") + "/" + self._namespace

    def _close_namespace(self, timeout=60):
        future = self._driver.release_namespace()
        future.result(timeout)


class ShareHandle:
    def __init__(self, backend):
        self.backend = backend

    def _context(self):
        backend = self.backend
        context = backend.context
        if context._closing or context._closed_event.is_set():
            from ...errors import ClosedContextError
            raise ClosedContextError("Context is closed")
        context._check_public_caller()
        if (getattr(backend, "local_path", ()) or getattr(backend, "readonly", False)
                or getattr(backend, "_conversion", False)
                or getattr(backend, "_conversion_steps", ())):
            raise AttributeError("Only whole Context cell nodes can be shared")
        try:
            backend._node()
        except StaleWorkflowHandleError as exc:
            raise NodeError("Shares require an existing whole cell node") from exc
        return context, tuple(backend.node_path)

    def __call__(self, path=None, readonly=True, *, mimetype=None, toplevel=False):
        context, node_path = self._context()
        shares = context.shares
        with shares._lock:
            context, node_path = self._context()
            if path is None:
                path = "/".join(node_path)
            spec = ShareSpec(path, readonly, mimetype, toplevel)
            celltype = context._controller.call(
                "_mount_validate", node_path, spec, klass=4
            )
            driver = shares._driver
            registration = driver.reserve(
                spec, celltype, uuid4().hex, make_sink(context._controller)
            )
            try:
                driver.start()
                observation = driver.initial_read(registration).result()
                try:
                    initial = context._controller.call(
                        "_mount_attach", node_path, spec, registration, observation, klass=2
                    )
                finally:
                    observation.release()
                initial.result(driver.delivery_timeout)
            except BaseException:
                detached = False
                try:
                    cleanup = context._controller.call(
                        "_mount_detach", node_path, "share", registration=registration, klass=2
                    )
                    if cleanup is not None:
                        cleanup.result(driver.delivery_timeout)
                        detached = True
                except Exception:
                    pass
                if not detached:
                    try:
                        driver.unregister(registration).result(driver.delivery_timeout)
                    except Exception:
                        pass
                try:
                    driver.release_namespace().result(driver.delivery_timeout)
                except Exception:
                    pass
                raise
        return None

    def unshare(self):
        context, node_path = self._context()
        shares = context.shares
        with shares._lock:
            context, node_path = self._context()
            future = context._controller.call("_mount_detach", node_path, "share", klass=2)
            if future is not None:
                future.result(shares._driver.delivery_timeout)

    @property
    def spec(self):
        context, node_path = self._context()
        node = context._node_snapshot(node_path)
        return None if node is None else node.attachments.get("share")

    @property
    def url(self):
        spec = self.spec
        if spec is None:
            return None
        context = self.backend.context
        base = context.shares._driver.url
        if base is None:
            return None
        registration = context._mount_sessions.get((tuple(self.backend.node_path), "share"))
        route = registration.registration.route if registration is not None else None
        if route is None:
            route = "/" + (spec.path if spec.toplevel else
                           context.shares.namespace + "/" + spec.path)
        return base.rstrip("/") + route

    @property
    def status(self):
        context, node_path = self._context()
        return context._controller.call("_mount_status", node_path, "share", klass=4)

    @property
    def error(self):
        status = self.status
        return None if status is None else status["error"]

    def clear_error(self):
        context, node_path = self._context()
        return context._controller.call("_mount_clear_error", node_path, "share", klass=2)
