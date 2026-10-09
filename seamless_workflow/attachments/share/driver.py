"""Mount-driver adapter for the context-free share transport."""

from concurrent.futures import Future

from ..policy import ABSENT
from ..session import Observation
from . import server as server_module


class ShareDriver:
    def __init__(self, namespace="ctx", owner=None):
        self.namespace = namespace
        self.owner = object() if owner is None else owner
        self._server = None

    @property
    def server(self):
        if self._server is not None and self._server._closed:
            self._server = None
        return self._server

    @property
    def delivery_timeout(self):
        server = self.server
        return 60 if server is None else server.delivery_timeout

    @property
    def url(self):
        server = self.server or server_module.get_server_if_started()
        return None if server is None else server.url

    def reserve(self, spec, celltype, session_id, sink, *, replaces=(), staged=False):
        server = self.server
        if server is None:
            server = server_module.get_server(start=False)
            self._server = server
        registration = server.reserve(
            self.namespace, spec, celltype, session_id, sink, owner=self.owner,
            replaces=replaces, staged=staged,
        )
        registration.service = self
        return registration

    def start(self):
        if self.server is None:
            self._server = server_module.get_server(start=False)
        return self._server.start()

    def initial_read(self, registration):
        future = Future()
        future.set_result(Observation(
            registration.session_id, 0, None, ABSENT, no_value=True
        ))
        return future

    def activate(self, registration):
        return registration.server.activate(registration)

    def deliver(self, registration, delivery):
        return registration.server.deliver(registration, delivery)

    def request_cut(self, registration, cut_id):
        return registration.server.request_cut(registration, cut_id)

    def poll(self, registration):
        return registration.server.poll(registration)

    def unregister(self, registration, *, delete=False, expected=None,
                   close_namespace=False):
        return registration.server.unregister(
            registration, close_namespace=close_namespace
        )

    def release_namespace(self):
        server = self.server or server_module.get_server_if_started()
        if server is None:
            future = Future()
            future.set_result(None)
            return future
        return server.release_namespace(self.namespace, self.owner)
