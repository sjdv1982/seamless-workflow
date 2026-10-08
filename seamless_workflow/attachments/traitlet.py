"""Traitlets hub for the workflow widget attachment."""

import logging
from collections import deque
from threading import RLock, get_ident

from traitlets import All, Any, HasTraits


_logger = logging.getLogger(__name__)


def _same(left, right):
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:
        return False


class Link:
    """A strong, in-process link between a hub and a traitlets object."""

    def __init__(self, hub, target, target_attr, *, bidirectional, initial_direction):
        if not callable(getattr(target, "observe", None)) or not callable(getattr(target, "unobserve", None)):
            raise TypeError("Widget links require a traitlets object")
        self.hub = hub
        self.target = target
        self.target_attr = target_attr
        self.bidirectional = bidirectional
        self._lock = RLock()
        self._active_thread = None
        self._queue = deque()
        self._linked = False
        try:
            HasTraits.observe(hub, self._from_hub, names="value")
            if bidirectional:
                target.observe(self._from_target, names=target_attr)
            self._linked = True
            hub._keep_link(self)
            if initial_direction == "cell":
                value = hub.value
                if value is not None:
                    self._write_target(value)
            elif initial_direction == "widget":
                value = getattr(target, target_attr)
                if value is not None:
                    self._write_source(value)
        except Exception:
            self.unlink()
            raise

    def _write_target(self, value):
        self._dispatch("hub", value)

    def _write_source(self, value):
        self._dispatch("target", value)

    def _dispatch(self, direction, value):
        if value is None:
            return
        ident = get_ident()
        with self._lock:
            if not self._linked:
                return
            if self._active_thread == ident:
                nested = True
            else:
                nested = False
            if self._active_thread is not None:
                if not nested:
                    self._queue.append((direction, value))
                    return
            else:
                self._active_thread = ident

        if nested:
            # Suppress only an echo that already agrees at both endpoints.
            # A nested coercion may instead need to flow back to the widget
            # that originated this change.
            try:
                equal = _same(
                    self.hub.value, getattr(self.target, self.target_attr)
                )
            except Exception:
                equal = False
            if equal:
                return
            with self._lock:
                if not self._linked:
                    return
                self._queue.append((direction, value))
            return

        current = (direction, value)
        while current is not None:
            try:
                self._process(*current)
            except Exception:
                with self._lock:
                    self._active_thread = None
                    self._queue.clear()
                raise
            with self._lock:
                if not self._linked or not self._queue:
                    self._active_thread = None
                    self._queue.clear()
                    return
                current = self._queue.popleft()

    def _process(self, direction, value):
        if direction == "hub":
            # A preceding link may have coerced the hub while the original
            # traitlets notification is still being delivered. Use its current
            # value so later links converge on that coerced value.
            value = self.hub.value
            if value is None:
                return
            try:
                setattr(self.target, self.target_attr, value)
            except Exception:
                _logger.exception("Widget link target refused a value")
                return
            if self.bidirectional:
                coerced = getattr(self.target, self.target_attr)
                if coerced is not None and not _same(coerced, value):
                    self.hub.value = coerced
        else:
            # A queued target event can be superseded by a later edit before
            # this dispatcher drains it.
            value = getattr(self.target, self.target_attr)
            if value is not None:
                self.hub.value = value

    def _from_hub(self, change):
        value = change["new"]
        if value is None:
            return
        self._dispatch("hub", value)

    def _from_target(self, change):
        value = change["new"]
        if value is None:
            return
        self._dispatch("target", value)

    def unlink(self):
        with self._lock:
            if not self._linked:
                return
            self._linked = False
            self._queue.clear()
        try:
            HasTraits.unobserve(self.hub, self._from_hub, names="value")
        except Exception:
            pass
        if self.bidirectional:
            try:
                self.target.unobserve(self._from_target, names=self.target_attr)
            except Exception:
                pass
        self.hub._drop_link(self)


class CellTraitlet(HasTraits):
    """One attached value shared by zero or more linked widgets."""

    value = Any(allow_none=True)

    def __init__(self, cell, context, path):
        super().__init__()
        self._cell = cell
        self._context = context
        self._path = path
        self._driver = None
        self._registration = None
        self._mode = "w"
        self._links = set()
        self._lock = RLock()
        self._upgrading = False
        self._destroyed = False

    def _keep_link(self, link):
        with self._lock:
            self._links.add(link)

    def _drop_link(self, link):
        with self._lock:
            self._links.discard(link)

    def _check_public(self):
        self._context._check_public_caller()
        if self._context._closed_event.is_set():
            from ..errors import ClosedContextError

            raise ClosedContextError("Context closed")

    def _cell_has_value(self):
        node = self._context._node_snapshot(self._path)
        return node.state == "complete"

    def _attach(self, mode):
        from .widget import WidgetDriver

        driver = WidgetDriver(self)
        self._driver = driver
        driver.attach(self._cell, mode=mode, authority="cell")
        self._registration = driver.registration
        self._mode = mode
        self._destroyed = False
        return driver

    def _detach(self):
        ctx = self._context
        ctx._check_public_caller()
        registration = self._registration
        if registration is None:
            self._seamless_detached()
            return
        future = ctx._controller.call(
            "_mount_detach", self._path, registration=registration, klass=2
        )
        if future is not None:
            timeout = self._driver.delivery_timeout if self._driver is not None else 60.
            future.result(timeout)
        else:
            self._seamless_detached()

    def _upgrade_to_rw(self):
        if "r" in self._mode:
            return
        old_value = self.value
        self._upgrading = True
        try:
            self._detach()
            self._driver = None
            try:
                self._attach("rw")
            except Exception:
                self.value = old_value
                self._driver = None
                self._attach("w")
                self._mode = "w"
                raise
        finally:
            self._upgrading = False

    def link(self, target, target_attr="value"):
        self._check_public()
        if self._destroyed:
            raise RuntimeError("Widget hub is detached")
        cell_has_value = self._cell_has_value()
        old_value = self.value
        if "r" not in self._mode:
            if not cell_has_value and self.value is None:
                target_value = getattr(target, target_attr)
                if target_value is not None:
                    self.value = target_value
            try:
                self._upgrade_to_rw()
            except Exception:
                if not cell_has_value:
                    self.value = old_value
                raise
        direction = "cell" if cell_has_value or old_value is not None else "widget"
        link = Link(self, target, target_attr, bidirectional=True, initial_direction=direction)
        return link

    def connect(self, target, target_attr="value"):
        self._check_public()
        if self._destroyed:
            raise RuntimeError("Widget hub is detached")
        link = Link(self, target, target_attr, bidirectional=False, initial_direction="cell")
        return link

    def observe(self, handler, names=All, type="change"):
        self._check_public()
        if self._destroyed:
            raise RuntimeError("Widget hub is detached")
        HasTraits.observe(self, handler, names=names, type=type)
        if self.value is None:
            return
        observes_value = names is All or names is None or names == "value"
        if not observes_value and isinstance(names, (tuple, list, set, frozenset)):
            observes_value = "value" in names
        if observes_value:
            handler({"name": "value", "old": None, "new": self.value,
                     "owner": self, "type": "change"})

    def _seamless_observe(self, handler, names="value"):
        """Register an internal transport callback without public initial delivery."""
        HasTraits.observe(self, handler, names=names)

    def unobserve(self, handler=None, names=All, type="change"):
        self._check_public()
        HasTraits.unobserve(self, handler, names=names, type=type)

    def unobserve_all(self, names=All):
        self._check_public()
        HasTraits.unobserve_all(self, names=names)

    def _seamless_unobserve(self, handler, names="value"):
        HasTraits.unobserve(self, handler, names=names)

    @property
    def status(self):
        self._check_public()
        if self._registration is None:
            return None
        return self._context._controller.call(
            "_mount_status", self._path, registration=self._registration, klass=4
        )

    @property
    def error(self):
        status = self.status
        return None if status is None else status["error"]

    def clear_error(self):
        self._check_public()
        if self._registration is not None:
            self._context._controller.call(
                "_mount_clear_error", self._path,
                registration=self._registration, klass=2
            )

    def destroy(self):
        self._check_public()
        if self._destroyed:
            return
        self._detach()

    def _seamless_detached(self):
        with self._lock:
            self._driver = None
            if self._upgrading:
                return
            self._destroyed = True
            links = tuple(self._links)
            self._links.clear()
        for link in links:
            link.unlink()
        HasTraits.unobserve_all(self, "value")
