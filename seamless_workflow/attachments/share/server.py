"""Context-free HTTP and WebSocket transport for shared cell values.

The transport owns checksums and marker state.  It deliberately never asks a
cell to compute: GET resolves only the checksum already installed by a
delivery, and PUT reports an observation to the registration's sink.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib.resources import files
import inspect
import os
import threading
from urllib.parse import quote

from seamless import Buffer, Checksum
from seamless.checksum.canonical import canon_T
from seamless.checksum.null import NULL_CHECKSUM

from ..session import DeliveryAck, MountLease, Observation
from .mime import content_type as infer_content_type, is_binary
from .spec import (
    RESERVED_NAMESPACE_KEYS,
    RESERVED_TOPLEVEL_KEYS,
    STATE_GRAPH_KEY,
)


_HANDSHAKE = ["Seamless share update server", "1.0"]
_RESERVED_TOPLEVEL = RESERVED_TOPLEVEL_KEYS
STATE_GRAPH_INTERVAL = 0.5
WSMsgType = None
web = None


def _load_aiohttp():
    """Load the optional HTTP stack when a share server is actually needed."""
    global WSMsgType, web
    if WSMsgType is not None and web is not None:
        return
    try:
        from aiohttp import WSMsgType as ws_msg_type, web as aiohttp_web
    except ImportError as exc:
        raise ImportError(
            "HTTP shares require aiohttp; install seamless-workflow[share]."
        ) from exc
    WSMsgType = ws_msg_type
    web = aiohttp_web


def _checksum_hex(value):
    if value is None:
        return None
    if isinstance(value, Checksum):
        return value.hex()
    if hasattr(value, "hex") and callable(value.hex):
        return value.hex()
    return Checksum(value).hex()


def _null_hex():
    return Checksum(NULL_CHECKSUM).hex()


def _completed_future(result=None, error=None):
    future = Future()
    if error is None:
        future.set_result(result)
    else:
        future.set_exception(error)
    return future


def _safe_segment(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        and "?" not in value
        and "#" not in value
    )


@dataclass(eq=False)
class _WsClient:
    ws: web.WebSocketResponse
    initializing: bool = True
    pending: list = field(default_factory=list)
    state_graph_sent: str | None = None


@dataclass
class _Namespace:
    name: str
    owner: object
    records: dict = field(default_factory=dict)
    clients: set = field(default_factory=set)
    staged_claim: bool = False
    state_graph: object = None


@dataclass
class ShareRegistration:
    """One reserved URL and its installed checksum state."""

    server: object
    namespace: str
    owner: object
    spec: object
    celltype: str
    session_id: str
    sink: object
    route: str
    content_type: str
    binary: bool
    node_path: tuple = ()
    staged: bool = False
    replaces: tuple = ()
    active: bool = False
    closed: bool = False
    checksum: str | None = None
    marker: int = 0
    fingerprint: object = None
    ws: int = 0
    lease: object = None
    pending_puts: set = field(default_factory=set)


class ShareServer:
    """A single-port HTTP/WebSocket service for shared values.

    ``reserve`` is synchronous and does not bind a socket.  ``activate`` starts
    the listener lazily, which keeps failed reservations free of threads,
    sockets, and checksum claims.
    """

    def __init__(
        self,
        host="0.0.0.0",
        port=5813,
        *,
        max_body_size=1024 * 1024 * 1024,
        put_timeout=60,
        get_timeout=60,
        delivery_timeout=60,
        resolver=None,
    ):
        self.host = host
        self.port = int(port)
        self.max_body_size = int(max_body_size)
        self.put_timeout = float(put_timeout)
        self.get_timeout = float(get_timeout)
        self.delivery_timeout = float(delivery_timeout)
        self.resolver = resolver if resolver is not None else self._default_resolver
        self.url = None
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._started = threading.Event()
        self._startup_error = None
        self._thread = None
        self._loop = None
        self._runner = None
        self._site = None
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="shareserver-worker")
        self._namespaces = {}
        self._top_records = {}
        self._records = {}
        self._staged_reservations = {}
        self._marker_floors = {}
        self._fingerprints = 0
        self._closed = False
        self._state_graph_task = None

    @staticmethod
    def _default_resolver(checksum):
        async def resolve():
            return await Checksum(checksum).resolution()
        return resolve()

    def _next_fingerprint(self):
        with self._lock:
            self._fingerprints += 1
            return self._fingerprints

    def _route_for(self, namespace, spec):
        path = getattr(spec, "path", None)
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise ValueError("share path must be a non-empty relative path")
        parts = path.split("/")
        if not all(_safe_segment(part) for part in parts):
            raise ValueError("share path contains an unsafe URL segment")
        if getattr(spec, "toplevel", False):
            if len(parts) != 1:
                raise ValueError("top-level share path must have one URL segment")
            if len(parts) == 1 and parts[0] in _RESERVED_TOPLEVEL:
                raise ValueError("top-level route is reserved")
            return "/" + "/".join(quote(part, safe="-._~") for part in parts)
        if len(parts) == 1 and parts[0] in RESERVED_NAMESPACE_KEYS:
            raise ValueError("namespace route is reserved")
        return "/" + quote(namespace, safe="-._~") + "/" + "/".join(
            quote(part, safe="-._~") for part in parts
        )

    def _check_reservation_conflicts_locked(self, namespace, spec, route, owner,
                                            replaces=(), *, ignore_staged=()):
        replaced_ids = {id(reg) for reg in replaces}
        ignored_ids = {id(reg) for reg in ignore_staged}
        path = spec.path
        is_top = bool(getattr(spec, "toplevel", False))
        ns = self._namespaces.get(namespace)
        if ns is not None and (owner is None or ns.owner is not owner):
            raise ValueError(f"namespace {namespace!r} is in use")
        namespace_top = self._top_records.get(namespace)
        if namespace_top is not None and id(namespace_top) not in replaced_ids:
            raise ValueError(f"namespace {namespace!r} conflicts with a top-level share")
        if ns is not None:
            same_key = ns.records.get(path)
            if same_key is not None and id(same_key) not in replaced_ids:
                raise ValueError(f"share key {path!r} is already in use in namespace {namespace!r}")
        staged_top = next((other for other in self._staged_reservations.values()
                           if id(other) not in ignored_ids
                           and id(other) not in replaced_ids
                           and other.spec.toplevel and other.spec.path == namespace), None)
        if staged_top is not None:
            raise ValueError(f"namespace {namespace!r} conflicts with a top-level share")
        if is_top:
            top_record = self._top_records.get(path)
            if top_record is not None and id(top_record) not in replaced_ids:
                raise ValueError(f"share URL {route!r} is in use")
            first, _, rest = path.partition("/")
            if first == namespace or first in self._namespaces:
                raise ValueError(f"share URL {route!r} conflicts with namespace {first!r}")
            nested_record = self._records.get((first, rest)) if rest else None
            if nested_record is not None and id(nested_record) not in replaced_ids:
                raise ValueError(f"share URL {route!r} is in use")
        else:
            record = self._records.get((namespace, path))
            if record is not None and id(record) not in replaced_ids:
                raise ValueError(f"share URL {route!r} is in use")
            full_key = f"{namespace}/{path}"
            top_record = self._top_records.get(full_key)
            if top_record is not None and id(top_record) not in replaced_ids:
                raise ValueError(f"share URL {route!r} is in use")
        for other in self._staged_reservations.values():
            if id(other) in ignored_ids or id(other) in replaced_ids:
                continue
            same_key = other.namespace == namespace and other.spec.path == path
            if other.route == route or same_key:
                raise ValueError(f"share URL {route!r} is in use")
            if is_top and other.namespace == path:
                raise ValueError(f"share URL {route!r} conflicts with namespace {path!r}")

    def reserve(self, namespace, spec, celltype, session_id, sink, owner=None, *,
                replaces=(), staged=False, node_path=(), state_graph=None):
        """Atomically reserve a URL, without starting the server or claiming data."""
        if not _safe_segment(namespace):
            raise ValueError("namespace must be one safe URL segment")
        if namespace in _RESERVED_TOPLEVEL:
            raise ValueError(f"namespace {namespace!r} is reserved")
        if self._closed:
            raise RuntimeError("share server is closed")
        route = self._route_for(namespace, spec)
        is_top = bool(getattr(spec, "toplevel", False))
        path = spec.path
        content_type = infer_content_type(celltype, path, getattr(spec, "mimetype", None))
        binary = is_binary(content_type)
        _load_aiohttp()
        replaces = tuple(replaces)
        with self._lock:
            self._check_reservation_conflicts_locked(
                namespace, spec, route, owner, replaces
            )
            ns = self._namespaces.get(namespace)
            if ns is None:
                ns = _Namespace(namespace, owner if owner is not None else object())
                ns.staged_claim = bool(staged)
                self._namespaces[namespace] = ns
            if state_graph is not None:
                ns.state_graph = state_graph
            reg = ShareRegistration(
                server=self,
                namespace=namespace,
                owner=ns.owner,
                spec=spec,
                celltype=celltype,
                session_id=session_id,
                sink=sink,
                route=route,
                content_type=content_type,
                binary=binary,
                node_path=tuple(node_path or ()),
                staged=bool(staged),
                replaces=replaces,
                marker=self._marker_floors.get(route, 0),
            )
            if staged:
                self._staged_reservations[id(reg)] = reg
            else:
                if is_top:
                    self._top_records[path] = reg
                    ns.records[path] = reg
                else:
                    self._records[(namespace, path)] = reg
                    ns.records[path] = reg
            return reg

    def commit_reservations(self, registrations):
        """Publish a prepared batch, swapping any same-owner live URLs atomically."""
        registrations = tuple(registrations)
        if not registrations:
            return registrations
        with self._lock:
            if self._closed:
                raise RuntimeError("share server is closed")
            for reg in registrations:
                if reg.server is not self or reg.closed or not reg.staged:
                    raise RuntimeError("share reservation is not staged")
                if self._staged_reservations.get(id(reg)) is not reg:
                    raise RuntimeError("share reservation is no longer available")
                self._check_reservation_conflicts_locked(
                    reg.namespace, reg.spec, reg.route, reg.owner, reg.replaces,
                    ignore_staged=registrations,
                )
            for index, reg in enumerate(registrations):
                for other in registrations[index + 1:]:
                    same_key = reg.namespace == other.namespace and reg.spec.path == other.spec.path
                    namespace_collision = (
                        (reg.spec.toplevel and reg.spec.path == other.namespace)
                        or (other.spec.toplevel and other.spec.path == reg.namespace)
                    )
                    if reg.route == other.route or same_key or namespace_collision:
                        raise ValueError(f"share URL {reg.route!r} conflicts with another graph share")
            replaced = {}
            for reg in registrations:
                ns = self._namespaces[reg.namespace]
                same_key = ns.records.get(reg.spec.path)
                current = (self._top_records.get(reg.spec.path) if reg.spec.toplevel
                           else self._records.get((reg.namespace, reg.spec.path)))
                for old in (same_key, current):
                    if old is not None:
                        replaced[id(old)] = old
                        reg.marker = max(reg.marker, old.marker)
                reg.marker = max(reg.marker, self._marker_floors.get(reg.route, 0))
                if reg.spec.toplevel:
                    self._top_records[reg.spec.path] = reg
                else:
                    self._records[(reg.namespace, reg.spec.path)] = reg
                ns.records[reg.spec.path] = reg
                ns.staged_claim = False
                reg.staged = False
                self._staged_reservations.pop(id(reg), None)
            # The new routes are visible before the old registrations release
            # their checksum leases. Their identity checks keep cleanup from
            # removing the just-published records.
            for reg in replaced.values():
                self._remove_registration(reg)
            return registrations

    def start(self):
        """Bind the configured listener and return once it is accepting requests."""
        _load_aiohttp()
        with self._start_lock:
            if self._closed:
                raise RuntimeError("share server is closed")
            if self._thread is None:
                self._started.clear()
                self._startup_error = None
                self._thread = threading.Thread(target=self._thread_main, name="seamless-shareserver", daemon=True)
                self._thread.start()
            thread = self._thread
        if not self._started.wait(15):
            raise RuntimeError("share server did not start")
        if self._closed:
            raise RuntimeError("share server is closed")
        if self._startup_error is not None:
            error = self._startup_error
            thread.join(timeout=2)
            with self._start_lock:
                if self._thread is thread:
                    self._thread = None
                    self._loop = None
            raise error
        return self

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_start())
        except BaseException as exc:
            self._startup_error = exc
            self._started.set()
            try:
                loop.run_until_complete(self._async_cleanup())
            except BaseException:
                pass
            loop.close()
            return
        self._started.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _async_start(self):
        _load_aiohttp()
        app = web.Application(client_max_size=self.max_body_size)
        app.router.add_route("*", "/", self._handle)
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        sockets = self._site._server.sockets
        bound_port = sockets[0].getsockname()[1] if sockets else self.port
        self.port = bound_port
        display_host = "localhost" if self.host in {"0.0.0.0", "::", "[::]"} else self.host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        self.url = f"http://{display_host}:{bound_port}"
        self._state_graph_task = asyncio.create_task(self._state_graph_loop())

    async def _async_cleanup(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _submit(self, coroutine):
        try:
            self.start()
        except BaseException as exc:
            coroutine.close()
            return _completed_future(error=exc)
        if self._closed:
            coroutine.close()
            return _completed_future(error=RuntimeError("share server is closed"))
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def activate(self, registration):
        return self._submit(self._activate(registration))

    async def _activate(self, registration):
        with self._lock:
            if registration.closed:
                raise RuntimeError("share registration is closed")
            if registration.active:
                return registration
            registration.active = True
            ns = self._namespaces.get(registration.namespace)
        if ns is not None:
            await self._broadcast_shares(ns)
        return registration

    def deliver(self, registration, delivery):
        return self._submit(self._deliver(registration, delivery))

    async def _deliver(self, reg, delivery):
        changed = False
        with self._lock:
            if reg.closed or not reg.active:
                ack = DeliveryAck(
                    reg.session_id,
                    delivery.seq,
                    reg.ws,
                    "error",
                    reg.checksum,
                    reg.fingerprint,
                    "share is detached",
                )
            elif delivery.expected_fingerprint != reg.fingerprint:
                ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, "conflict", reg.checksum, reg.fingerprint)
            else:
                checksum = _checksum_hex(delivery.checksum)
                old_checksum = reg.checksum
                if checksum != old_checksum:
                    new_lease = self._lease_for(checksum, reg, "record")
                    old_lease = reg.lease
                    reg.lease = new_lease
                    reg.checksum = checksum
                    reg.marker += 1
                    self._marker_floors[reg.route] = reg.marker
                    if old_lease is not None:
                        old_lease._release_refholds()
                    reg.ws += 1
                    self._next_fingerprint()
                    reg.fingerprint = self._fingerprints
                else:
                    reg.ws += 1
                    reg.fingerprint = self._next_fingerprint()
                ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, "written", reg.checksum, reg.fingerprint)
                changed = old_checksum != reg.checksum
        try:
            reg.sink("_mount_delivered", ack)
        except Exception:
            pass
        if ack.outcome == "written" and changed:
            await self._broadcast_update(reg)
        return ack

    def _lease_for(self, checksum, reg, suffix):
        if checksum is None or checksum == _null_hex():
            return None
        return MountLease(Checksum(checksum), f"share-server:{reg.namespace}:{reg.spec.path}:{suffix}", reg.celltype)

    def request_cut(self, registration, cut_id):
        return self._submit(self._request_cut(registration, cut_id))

    async def _request_cut(self, reg, cut_id):
        with self._lock:
            if reg.closed:
                return None
            pending = tuple(reg.pending_puts)
        if pending:
            await asyncio.gather(*(self._wait_pending(item) for item in pending), return_exceptions=True)
        with self._lock:
            if reg.closed:
                return None
            payload = (reg.session_id, cut_id, reg.ws)
            sink = reg.sink
        return sink("_mount_cut", payload)

    async def _wait_pending(self, value):
        if isinstance(value, Future):
            await asyncio.wrap_future(value)
        elif inspect.isawaitable(value):
            await value

    def poll(self, registration, force=False):
        return self._submit(self._poll(registration, force))

    async def _poll(self, registration, force=False):
        return None

    def unregister(self, registration, close_namespace=False):
        if self._closed:
            return _completed_future(None)
        if self._loop is None or not self._started.is_set() or self._startup_error is not None:
            self._remove_registration(registration)
            if close_namespace:
                self._release_namespace(registration.namespace, registration.owner)
            return _completed_future(None)
        return self._submit(self._unregister(registration, close_namespace))

    def release_namespace(self, namespace, owner):
        if self._closed:
            return _completed_future(None)
        if self._loop is None or not self._started.is_set() or self._startup_error is not None:
            self._release_namespace(namespace, owner)
            return _completed_future(None)
        return self._submit(self._close_namespace(namespace, owner))

    async def _close_namespace(self, namespace, owner):
        with self._lock:
            ns = self._namespaces.get(namespace)
            if ns is None or ns.owner is not owner or ns.records:
                return
            clients = tuple(ns.clients)
        for client in clients:
            try:
                await client.ws.close(code=1001, message=b"namespace released")
            except Exception:
                pass
        with self._lock:
            ns = self._namespaces.get(namespace)
            if ns is not None and ns.owner is owner and not ns.records:
                ns.clients.clear()
                self._namespaces.pop(namespace, None)

    async def _unregister(self, reg, close_namespace=False):
        with self._lock:
            ns = self._namespaces.get(reg.namespace)
            owns_namespace = ns is not None and ns.owner is reg.owner
            if close_namespace:
                regs = list(ns.records.values()) if owns_namespace else []
                regs.extend(
                    item for item in self._staged_reservations.values()
                    if item.namespace == reg.namespace and item.owner is reg.owner
                )
                if not any(item is reg for item in regs):
                    regs.append(reg)
            else:
                regs = [reg]
        for item in regs:
            was_active = item.active
            self._remove_registration(item)
            if (not close_namespace and was_active and ns is not None
                    and ns.owner is item.owner):
                await self._broadcast_shares(ns)
        if close_namespace and owns_namespace:
            for client in tuple(ns.clients):
                try:
                    await client.ws.close(code=1001, message=b"namespace closed")
                except Exception:
                    pass
            ns.clients.clear()
            with self._lock:
                if (self._namespaces.get(reg.namespace) is ns
                        and ns.owner is reg.owner):
                    self._namespaces.pop(reg.namespace, None)
        return None

    def _remove_registration(self, reg):
        with self._lock:
            if reg.closed:
                return
            reg.closed = True
            reg.active = False
            was_staged = reg.staged
            self._staged_reservations.pop(id(reg), None)
            if getattr(reg.spec, "toplevel", False):
                if self._top_records.get(reg.spec.path) is reg:
                    self._top_records.pop(reg.spec.path, None)
                ns = self._namespaces.get(reg.namespace)
                if ns is not None and ns.records.get(reg.spec.path) is reg:
                    ns.records.pop(reg.spec.path, None)
            else:
                if self._records.get((reg.namespace, reg.spec.path)) is reg:
                    self._records.pop((reg.namespace, reg.spec.path), None)
                ns = self._namespaces.get(reg.namespace)
                if ns is not None and ns.records.get(reg.spec.path) is reg:
                    ns.records.pop(reg.spec.path, None)
            self._marker_floors[reg.route] = max(self._marker_floors.get(reg.route, 0), reg.marker)
            lease, reg.lease = reg.lease, None
            if lease is not None:
                lease._release_refholds()
            ns = self._namespaces.get(reg.namespace)
            if (was_staged and ns is not None and ns.staged_claim and not ns.records
                    and not any(item.namespace == reg.namespace
                                for item in self._staged_reservations.values())):
                self._namespaces.pop(reg.namespace, None)

    def _release_namespace(self, namespace, owner):
        with self._lock:
            ns = self._namespaces.get(namespace)
            if ns is not None and ns.owner is owner:
                self._namespaces.pop(namespace, None)

    async def _handle(self, request):
        if request.method == "OPTIONS":
            return self._response(status=204)
        if request.method not in {"GET", "HEAD", "PUT"}:
            return self._response(status=405, headers={"Allow": "GET, HEAD, PUT, OPTIONS"})
        raw_path = request.path.lstrip("/")
        if raw_path == "openapi.json":
            if request.method != "GET":
                return self._response(status=405, headers={"Allow": "GET, OPTIONS"})
            return self._response(status=200, json_data=self.openapi())
        if raw_path == "seamless-client.js":
            if request.method not in {"GET", "HEAD"}:
                return self._response(status=405, headers={"Allow": "GET, HEAD, OPTIONS"})
            asset = (files("seamless_workflow.attachments.share")
                     .joinpath("static", "seamless-client.js").read_bytes())
            return self._response(
                status=200,
                body=b"" if request.method == "HEAD" else asset,
                headers={
                    "Content-Type": "text/javascript; charset=utf-8",
                    "Cache-Control": "no-cache",
                },
            )
        if not raw_path:
            return self._redirect_index(None)
        trailing_slash = raw_path.endswith("/")
        path = raw_path[:-1] if trailing_slash else raw_path
        parts = path.split("/")
        if not path or any(not part for part in parts):
            return self._json_error(404, "no such share")
        if trailing_slash:
            if len(parts) == 1:
                return self._redirect_index(parts[0])
            return self._json_error(404, "no such share")
        with self._lock:
            top_reg = self._top_records.get(path)
        if top_reg is not None:
            if top_reg.active and not top_reg.closed:
                return await self._serve(request, top_reg)
            return self._json_error(404, "no such share")
        if len(parts) == 1:
            with self._lock:
                ns = self._namespaces.get(parts[0])
            if ns is not None and request.method == "GET" and request.headers.get("Upgrade", "").lower() == "websocket":
                return await self._websocket(request, ns)
            return self._json_error(404, "no such share")
        namespace, key = parts[0], "/".join(parts[1:])
        with self._lock:
            ns = self._namespaces.get(namespace)
            reg = self._records.get((namespace, key))
        if key == STATE_GRAPH_KEY:
            if ns is None:
                return self._json_error(404, "no such share")
            return await self._serve_state_graph(request, ns)
        if reg is not None and reg.active and not reg.closed:
            return await self._serve(request, reg)
        return self._json_error(404, "no such share")

    def _redirect_index(self, namespace):
        if namespace is None:
            with self._lock:
                reg = self._top_records.get("index.html")
            if reg is not None and reg.active and not reg.closed:
                return self._response(status=302, headers={"Location": "/index.html"})
        else:
            with self._lock:
                reg = self._records.get((namespace, "index.html"))
            if reg is not None and reg.active and not reg.closed:
                return self._response(status=302, headers={"Location": f"/{quote(namespace, safe='-._~')}/index.html"})
        return self._json_error(404, "no such share")

    async def _serve(self, request, reg):
        if request.method == "PUT":
            return await self._put(request, reg)
        mode = request.query.get("mode")
        if mode not in (None, "value", "checksum"):
            return self._json_error(400, "invalid mode")
        with self._lock:
            checksum, marker = reg.checksum, reg.marker
        if checksum is None:
            return self._json_error(404, "share has no value")
        etag = f'"{checksum}"'
        headers = self._record_headers(reg, checksum, marker)
        if mode == "checksum":
            headers = dict(headers)
            headers["Content-Type"] = "text/plain; charset=utf-8"
            headers["Content-Length"] = str(len(checksum))
        inm = request.headers.get("If-None-Match")
        if inm and (inm.strip() == "*" or etag in {part.strip() for part in inm.split(",")}):
            return self._response(status=304, headers=headers)
        if mode == "checksum":
            body = checksum.encode("ascii")
            if request.method == "HEAD":
                return self._response(status=200, headers=headers)
            return self._response(status=200, body=body, headers=headers)
        if checksum == _null_hex():
            return self._response(status=204, headers=headers)
        if request.method == "HEAD":
            return self._response(status=200, headers=headers)
        with self._lock:
            request_lease = self._lease_for(checksum, reg, "get")
        try:
            worker_future = self._executor.submit(self._resolve_value, checksum)
        except Exception as exc:
            if request_lease is not None:
                request_lease._release_refholds()
            return self._json_error(503, str(exc) or "value resolution failed", headers=headers)
        if request_lease is not None:
            worker_future.add_done_callback(lambda _, lease=request_lease: lease._release_refholds())
        try:
            body = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(worker_future)), timeout=self.get_timeout
            )
        except asyncio.TimeoutError:
            return self._json_error(503, "value resolution timed out", headers=headers)
        except Exception as exc:
            return self._json_error(503, str(exc) or "value resolution failed", headers=headers)
        return self._response(status=200, body=body, headers=headers)

    async def _serve_state_graph(self, request, ns):
        if request.method == "PUT":
            return self._response(
                status=405, headers={"Allow": "GET, HEAD, OPTIONS"}
            )
        provider = ns.state_graph
        if provider is None:
            return self._json_error(404, "no such share")
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._executor, provider, self.get_timeout, False
            )
            publication = await asyncio.wait_for(
                asyncio.shield(future), timeout=self.get_timeout
            )
        except asyncio.TimeoutError:
            return self._json_error(503, "state graph request timed out")
        except Exception as exc:
            return self._json_error(
                503, str(exc) or "state graph request failed"
            )
        if publication is None:
            return self._json_error(404, "no such share")
        marker, digest, body = publication
        etag = f'"{digest}"'
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "ETag": etag,
            "X-Seamless-Marker": str(marker),
            "Cache-Control": "no-cache",
            "Content-Length": str(len(body)),
        }
        inm = request.headers.get("If-None-Match")
        if inm and (
            inm.strip() == "*"
            or etag in {part.strip() for part in inm.split(",")}
        ):
            return self._response(status=304, headers=headers)
        return self._response(
            status=200,
            body=b"" if request.method == "HEAD" else body,
            headers=headers,
        )

    def _record_headers(self, reg, checksum, marker):
        return {
            "ETag": f'"{checksum}"',
            "X-Seamless-Marker": str(marker),
            "Cache-Control": "no-cache",
            "Content-Type": reg.content_type,
        }

    async def _put(self, request, reg):
        if getattr(reg.spec, "readonly", False):
            return self._response(status=405, headers={"Allow": "GET, HEAD"})
        mode = request.query.get("mode")
        if mode not in (None, "value"):
            return self._json_error(400, "invalid mode")
        marker_arg = request.query.get("marker")
        if marker_arg is not None:
            try:
                expected_marker = int(marker_arg)
                if expected_marker < 0 or str(expected_marker) != marker_arg:
                    raise ValueError
            except (TypeError, ValueError):
                return self._json_error(400, "invalid marker")
        else:
            expected_marker = None
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return self._json_error(413, "body too large")
        if len(body) > self.max_body_size:
            return self._json_error(413, "body too large")
        try:
            canonical = await self._loop.run_in_executor(self._executor, canon_T, body, reg.celltype)
            buffer = Buffer(canonical)
            checksum = buffer.get_checksum().hex()
        except Exception as exc:
            return self._json_error(422, str(exc) or "invalid value")
        with self._lock:
            if reg.closed or not reg.active:
                return self._json_error(404, "share is closing")
            if expected_marker is not None and expected_marker != reg.marker:
                current = {"checksum": reg.checksum, "marker": reg.marker}
                return self._response(status=409, json_data=current)
            old_checksum = reg.checksum
            if checksum != old_checksum:
                new_lease = self._lease_for(checksum, reg, "record")
                old_lease = reg.lease
                reg.lease = new_lease
                reg.checksum = checksum
                reg.marker += 1
                self._marker_floors[reg.route] = reg.marker
                if old_lease is not None:
                    old_lease._release_refholds()
            reg.ws += 1
            reg.fingerprint = self._next_fingerprint()
            observation_ws = reg.ws
            fingerprint = reg.fingerprint
            marker = reg.marker
            ns = self._namespaces.get(reg.namespace)
            changed = old_checksum != checksum
            acceptance_fence = Future()
            reg.pending_puts.add(acceptance_fence)
        observation_lease = self._lease_for(checksum, reg, "observation")
        observation = Observation(
            session_id=reg.session_id,
            ws=observation_ws,
            fingerprint=fingerprint,
            checksum=checksum,
            buffer=buffer,
            leases=(() if observation_lease is None else (observation_lease,)),
            reason="share PUT",
            needs_canonical_write=False,
        )
        if changed and ns is not None:
            try:
                await self._broadcast_update(reg)
            except Exception:
                pass
        try:
            reply = reg.sink("_mount_observed", observation)
        except Exception as exc:
            self._put_reply_done(reg, acceptance_fence, observation)
            return self._json_error(503, str(exc) or "observation delivery failed")
        if isinstance(reply, Future):
            reply.add_done_callback(lambda done, r=reg, fence=acceptance_fence, obs=observation: self._put_reply_done(r, fence, obs))
        elif inspect.isawaitable(reply):
            task = asyncio.ensure_future(reply)
            task.add_done_callback(lambda done, r=reg, fence=acceptance_fence, obs=observation: self._put_reply_done(r, fence, obs))
        else:
            self._put_reply_done(reg, acceptance_fence, observation)
        if isinstance(reply, Future):
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(reply)), timeout=self.put_timeout)
            except asyncio.TimeoutError:
                return self._json_error(503, "observation installation timed out")
            except Exception as exc:
                return self._json_error(503, str(exc) or "observation installation failed")
        elif inspect.isawaitable(reply):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.put_timeout)
            except asyncio.TimeoutError:
                return self._json_error(503, "observation installation timed out")
            except Exception as exc:
                return self._json_error(503, str(exc) or "observation installation failed")
        return self._response(status=200, json_data={"checksum": checksum, "marker": marker})

    def _put_reply_done(self, reg, acceptance_fence, observation):
        observation.release()
        if not acceptance_fence.done():
            acceptance_fence.set_result(None)
        with self._lock:
            reg.pending_puts.discard(acceptance_fence)

    def _resolve_value(self, checksum):
        value = self.resolver(checksum)
        if inspect.isawaitable(value):
            value = asyncio.run(value)
        return self._as_bytes(value)

    @staticmethod
    def _as_bytes(value):
        if isinstance(value, Buffer):
            return bytes(value.content)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        if hasattr(value, "content"):
            content = value.content
            if isinstance(content, str):
                return content.encode("utf-8")
            return bytes(content)
        return bytes(value)

    async def _websocket(self, request, ns):
        ws = web.WebSocketResponse(heartbeat=10, autoping=True)
        await ws.prepare(request)
        client = _WsClient(ws)
        with self._lock:
            if self._namespaces.get(ns.name) is not ns:
                await ws.close(code=1001, message=b"namespace closed")
                return ws
            ns.clients.add(client)
        try:
            await ws.send_json(_HANDSHAKE)
            await ws.send_json(["shares", self._snapshot(ns)])
            client.initializing = False
            for message in client.pending:
                if not ws.closed:
                    await ws.send_json(message)
            client.pending.clear()
            async for message in ws:
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            with self._lock:
                ns.clients.discard(client)
        return ws

    def _snapshot(self, ns):
        with self._lock:
            items = tuple(ns.records.items())
        result = {}
        for key, reg in items:
            if reg.closed or not reg.active:
                continue
            result[key] = {
                "url": reg.route,
                "readonly": bool(getattr(reg.spec, "readonly", False)),
                "content_type": reg.content_type,
                "binary": reg.binary,
                "checksum": reg.checksum,
                "marker": reg.marker,
            }
        return result

    async def _queue_or_send(self, client, message):
        if client.initializing:
            client.pending.append(message)
        elif not client.ws.closed:
            await client.ws.send_json(message)

    async def _broadcast_shares(self, ns):
        message = ["shares", self._snapshot(ns)]
        for client in tuple(ns.clients):
            try:
                await self._queue_or_send(client, message)
            except Exception:
                ns.clients.discard(client)

    async def _broadcast_update(self, reg):
        ns = self._namespaces.get(reg.namespace)
        if ns is None:
            return
        message = ["update", [reg.spec.path, reg.checksum, reg.marker]]
        for client in tuple(ns.clients):
            try:
                await self._queue_or_send(client, message)
            except Exception:
                ns.clients.discard(client)

    async def _state_graph_loop(self):
        while True:
            await asyncio.sleep(STATE_GRAPH_INTERVAL)
            with self._lock:
                namespaces = tuple(
                    ns for ns in self._namespaces.values()
                    if ns.state_graph is not None and ns.clients
                )
            for ns in namespaces:
                await self._state_graph_tick(ns)

    async def _state_graph_tick(self, ns):
        with self._lock:
            if (self._namespaces.get(ns.name) is not ns
                    or ns.state_graph is None or not ns.clients):
                return
            provider = ns.state_graph
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._executor, provider, self.get_timeout, True
            )
            publication = await asyncio.wait_for(
                asyncio.shield(future), timeout=self.get_timeout
            )
        except Exception:
            return
        if publication is None:
            return
        marker, digest, _ = publication
        with self._lock:
            if self._namespaces.get(ns.name) is not ns:
                return
            clients = tuple(ns.clients)
        message = ["state-graph", [digest, marker]]
        for client in clients:
            if (client.initializing or client.ws.closed
                    or client.state_graph_sent == digest):
                continue
            client.state_graph_sent = digest
            try:
                await self._queue_or_send(client, message)
            except Exception:
                with self._lock:
                    ns.clients.discard(client)

    def _response(self, status=200, *, body=None, text=None, json_data=None, headers=None):
        response_headers = dict(headers or {})
        if json_data is not None:
            response = web.json_response(json_data, status=status, headers=response_headers)
        elif text is not None:
            response = web.Response(text=text, status=status, headers=response_headers)
        else:
            response = web.Response(body=body, status=status, headers=response_headers)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, PUT, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, If-None-Match, If-Match, X-Seamless-Marker"
        response.headers["Access-Control-Expose-Headers"] = "ETag, X-Seamless-Marker"
        return response

    def _json_error(self, status, message, headers=None):
        json_headers = dict(headers or {})
        json_headers.pop("Content-Type", None)
        return self._response(status=status, json_data={"error": str(message)}, headers=json_headers)

    def close(self):
        """Stop the listener, close clients, and release server-owned leases."""
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is not None and not self._started.is_set():
            self._started.wait(15)
        if self._loop is not None and self._started.is_set() and self._startup_error is None and thread is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(timeout=15)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                thread.join(timeout=15)
        else:
            for reg in self._all_registrations():
                self._remove_registration(reg)
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._thread = None
        self._loop = None

    async def _async_close(self):
        if self._state_graph_task is not None:
            self._state_graph_task.cancel()
            await asyncio.gather(self._state_graph_task, return_exceptions=True)
            self._state_graph_task = None
        for ns in tuple(self._namespaces.values()):
            for client in tuple(ns.clients):
                try:
                    await client.ws.close(code=1001, message=b"server closed")
                except Exception:
                    pass
            ns.clients.clear()
        for reg in self._all_registrations():
            self._remove_registration(reg)
        await self._async_cleanup()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _all_registrations(self):
        with self._lock:
            values = (list(self._top_records.values()) + list(self._records.values())
                      + list(self._staged_reservations.values()))
            return tuple({id(reg): reg for reg in values}.values())

    def openapi(self, namespace=None, owner=None):
        """Return an OpenAPI document from one locked snapshot of live shares."""
        from .openapi import build_openapi

        with self._lock:
            registrations = tuple(
                reg for reg in (*self._top_records.values(), *self._records.values())
                if reg.active and not reg.closed and not reg.staged
                and (namespace is None or reg.namespace == namespace)
                and (owner is None or reg.owner is owner)
            )
            namespaces = tuple(
                ns.name for ns in self._namespaces.values()
                if ns.state_graph is not None
                and (namespace is None or ns.name == namespace)
                and (owner is None or ns.owner is owner)
            )
            return build_openapi(registrations, namespaces=namespaces)


_default_lock = threading.Lock()
_default_server = None
_default_config = {
    "host": os.environ.get("SEAMLESS_SHARE_HOST", "0.0.0.0"),
    "port": int(os.environ.get("SEAMLESS_SHARE_PORT", "5813")),
}


def configure(host="0.0.0.0", port=5813, **kwargs):
    """Configure the lazy process-wide server used by the public facade."""
    global _default_config, _default_server
    stale = None
    with _default_lock:
        if _default_server is not None:
            server = _default_server
            with server._lock:
                unused = (
                    server._thread is None
                    and not server._namespaces
                    and not server._records
                    and not server._top_records
                )
            if not server._closed and server._startup_error is None and not unused:
                raise RuntimeError("share server is already initialized")
            stale, _default_server = server, None
        _default_config = {"host": host, "port": port, **kwargs}
    if stale is not None:
        stale.close()


def get_server(*, start=True):
    """Return and lazily bind the process-wide share server."""
    global _default_server
    with _default_lock:
        if _default_server is None:
            _default_server = ShareServer(**_default_config)
        server = _default_server
    if start:
        server.start()
    return server


def get_server_if_started():
    """Return the process server if one was created, without creating it."""
    with _default_lock:
        return _default_server


def close():
    """Stop and forget the process-wide share server."""
    global _default_server
    with _default_lock:
        server, _default_server = _default_server, None
    if server is not None:
        server.close()
