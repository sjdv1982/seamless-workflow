"""Shares contract transport tests, independent of Context/controller code.

The fake sink acknowledges sensed writes with explicit Futures. HTTP and the
websocket use real sockets; no tests in this file are skipped or xfailed.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import asyncio
import hashlib
import queue
import threading

import pytest
import requests
import numpy as np

from seamless import Buffer, Checksum
from seamless.checksum.null import NULL_CHECKSUM
from seamless_workflow.attachments.session import Delivery, MountLease
from test_contract_shares import get, put, marker, updates


@dataclass(frozen=True)
class Spec:
    path: str = "a"
    readonly: bool = False
    mimetype: str = None
    toplevel: bool = False
    driver: str = "share"
    authority: str = "cell"

    @property
    def mode(self):
        return "w" if self.readonly else "rw"


class Sink:
    def __init__(self):
        self.events = queue.Queue()
        self.observed = queue.Queue()
        self.held = False

    def __call__(self, operation, payload):
        reply = Future()
        if operation == "_mount_observed":
            reply.add_done_callback(lambda _: payload.release())
            self.observed.put((payload, reply))
            if not self.held:
                reply.set_result(None)
        else:
            reply.set_result(None)
        self.events.put((operation, payload))
        return reply

    def receive(self, operation):
        while True:
            name, payload = self.events.get(timeout=10)
            if name == operation:
                return payload

    def drain(self):
        while not self.events.empty():
            self.events.get_nowait()
        while not self.observed.empty():
            self.observed.get_nowait()


@pytest.fixture
def server():
    from seamless_workflow.attachments.share.server import ShareServer
    service = ShareServer(host="127.0.0.1", port=0)
    service.start()
    yield service
    service.close()


def attach(server, spec=None, celltype="int", namespace="ctx", session_id="s", sink=None, owner=None):
    sink = sink or Sink()
    registration = server.reserve(namespace, spec or Spec(), celltype, session_id, sink, owner=owner)
    server.activate(registration).result(10)
    return registration, sink, server.url + ("/" + registration.spec.path if registration.spec.toplevel else "/" + namespace + "/" + registration.spec.path)


def deliver(server, reg, sink, value=1, celltype=None, expected=None, seq=1):
    buffer = Buffer(value, celltype or reg.celltype)
    buffer.tempref()
    checksum = buffer.get_checksum()
    lease = MountLease(checksum, "test:share-transport:" + str(seq))
    try:
        server.deliver(reg, Delivery(reg.session_id, seq, checksum.hex(), reg.celltype, expected, lease)).result(10)
        acknowledgement = sink.receive("_mount_delivered")
        assert acknowledgement.outcome in ("written", "error", "conflict")
        return acknowledgement, checksum.hex(), buffer.content
    finally:
        lease._release_refholds()


def test_rest_record_and_head_without_resolution(server):
    """Reading; Record: delivery moves checksum; HEAD/ETag/checksum don't resolve."""
    calls = []
    def forbidden(checksum):
        calls.append(checksum)
        raise RuntimeError("unavailable")
    server.resolver = forbidden
    reg, sink, url = attach(server)
    assert get(url).status_code == 404
    ack, checksum, body = deliver(server, reg, sink)
    assert ack.outcome == "written" and calls == []
    head = requests.head(url, timeout=10)
    assert head.status_code == 200 and not head.content and calls == []
    assert head.headers["ETag"] == '"' + checksum + '"'
    assert get(url, params={"mode": "checksum"}).text == checksum and calls == []
    assert get(url, params={"mode": "checksum"}).headers["Content-Type"].split(";")[0] == "text/plain"
    assert get(url, headers={"If-None-Match": head.headers["ETag"]}).status_code == 304
    assert calls == []
    assert get(url).status_code == 503 and calls == [checksum]


def test_get_body_is_checksum_bytes(server):
    """Reading: byte-for-byte canonical buffer with matching SHA-256 and ETag."""
    reg, sink, url = attach(server, celltype="plain")
    ack, checksum, body = deliver(server, reg, sink, {"x": 1})
    response = get(url)
    assert response.status_code == 200 and response.content == body
    assert hashlib.sha256(body).hexdigest() == checksum
    assert response.headers["ETag"] == '"' + checksum + '"'
    assert response.headers["Cache-Control"] == "no-cache"
    assert get(url + "/x").status_code == 404
    assert get(url + "/").status_code == 404


@pytest.mark.parametrize("celltype,value,path,mimetype,content_type,binary", [
    ("text", "page", "index.html", None, "text/html", False),
    ("bytes", b"image", "image.png", None, "image/png", True),
    ("str", "string", "a", None, "application/json", False),
    ("text", "custom", "a", "application/vnd.test+json", "application/vnd.test+json", False),
    ("text", "custom", "a", "application/xml", "application/xml", False),
    ("mixed", {"x": 1}, "a", None, "application/octet-stream", True),
    ("text", "text", "a", None, "text/plain", False),
    ("python", "pass\n", "a", None, "text/x-python", False),
    ("ipython", "pass\n", "a", None, "text/x-python", False),
    ("yaml", "a: 1\n", "a", None, "application/yaml", False),
    ("plain", {}, "a", None, "application/json", False),
    ("int", 1, "a", None, "application/json", False),
    ("float", 1.5, "a", None, "application/json", False),
    ("bool", True, "a", None, "application/json", False),
    ("bytes", b"bytes", "a", None, "application/octet-stream", True),
    ("binary", np.array([1, 2]), "a", None, "application/octet-stream", True),
])
def test_content_types_and_websocket_binary_metadata(server, celltype, value, path, mimetype, content_type, binary):
    """Content types; Websocket: override/extension/default and binary classification."""
    reg, sink, url = attach(server, Spec(path, mimetype=mimetype), celltype)
    deliver(server, reg, sink, value)
    response = get(url)
    assert response.headers["Content-Type"].split(";")[0] == content_type
    if content_type.startswith("text/"):
        assert "charset=utf-8" in response.headers["Content-Type"].lower()
    with updates(server.url + "/ctx") as receive:
        metadata = receive("shares")[1][path]
        assert metadata["url"] == "/ctx/" + path
        assert metadata["binary"] is binary
        assert metadata["content_type"].split(";")[0] == content_type
        assert metadata["checksum"] == reg_checksum(response)


def reg_checksum(response):
    return response.headers["ETag"].strip('"')


def test_put_waits_for_installation_and_websocket_announces_at_acceptance(server):
    """Writing; Websocket: update at acceptance, HTTP 200 after sink completion."""
    reg, sink, url = attach(server)
    deliver(server, reg, sink)
    sink.drain()
    sink.held = True
    with updates(server.url + "/ctx") as receive, ThreadPoolExecutor(max_workers=1) as pool:
        assert receive() == ["Seamless share update server", "1.0"]
        snapshot = receive("shares")[1]["a"]
        pending = pool.submit(put, url, b"7")
        observation, installed = sink.observed.get(timeout=10)
        try:
            assert observation.checksum == Buffer(7, "int").get_checksum().hex()
            event = receive("update")[1]
            assert event == ["a", observation.checksum, snapshot["marker"] + 1]
            assert not pending.done()
            assert get(url).json() == 7
        finally:
            installed.set_result(None)
        response = pending.result(timeout=10)
        assert response.status_code == 200
        assert response.json() == {"checksum": observation.checksum, "marker": snapshot["marker"] + 1}


def test_canonical_put_and_noop_marker(server):
    """Writing; Record: canonical body installed once; equal writes preserve marker."""
    reg, sink, url = attach(server, celltype="plain")
    deliver(server, reg, sink, {"a": 0})
    before = get(url)
    accepted = put(url, b'{"a":1}')
    assert accepted.status_code == 200
    after = get(url)
    assert after.content == Buffer({"a": 1}, "plain").content
    assert marker(after) == marker(before) + 1
    again = put(url, b'{ "a" : 1 }')
    assert again.status_code == 200 and again.json() == accepted.json()


@pytest.mark.parametrize("body", [b"", b"null"])
def test_null_value(server, body):
    """Writing; Reading: empty/null request serves 204 with null checksum."""
    reg, sink, url = attach(server)
    assert put(url, body).status_code == 200
    response = get(url)
    assert response.status_code == 204 and not response.content
    assert response.headers["ETag"] == '"' + Checksum(NULL_CHECKSUM).hex() + '"'
    assert requests.head(url, timeout=10).status_code == 204


def test_invalid_request_never_reaches_sink(server):
    """Writing; Errors: invalid bodies/parameters leave record and sink unchanged."""
    reg, sink, url = attach(server)
    deliver(server, reg, sink)
    sink.drain()
    before = get(url)
    for body in (b"broken", b'"text"', b"{}"):
        response = put(url, body)
        assert response.status_code == 422 and response.json()["error"]
    for params in ({"marker": "bad"}, {"mode": "bad"}):
        assert put(url, b"2", params=params).status_code == 400
    assert sink.observed.empty()
    after = get(url)
    assert after.content == before.content and marker(after) == marker(before)


def test_cas_and_concurrent_cas(server):
    """Writing: compare-and-set is atomic and reports current record on 409."""
    reg, sink, url = attach(server)
    deliver(server, reg, sink)
    base = marker(get(url))
    barrier = threading.Barrier(2)
    def write(value):
        barrier.wait(timeout=10)
        return put(url, str(value), params={"marker": base})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, value) for value in (2, 3)]
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r for r in responses if r.status_code == 200).json()
    stale = next(r for r in responses if r.status_code == 409).json()
    assert winner == stale and winner["marker"] == base + 1
    assert put(url, b"9").status_code == 200


def test_put_beats_delivery_with_old_fingerprint(server):
    """Writing: PUT defeats a delivery prepared against an older record."""
    reg, sink, url = attach(server)
    initial, _, _ = deliver(server, reg, sink)
    accepted = put(url, b"5")
    assert accepted.status_code == 200
    conflict, _, _ = deliver(server, reg, sink, 2, expected=initial.fingerprint, seq=2)
    assert conflict.outcome == "conflict"
    assert get(url).json() == 5
    assert marker(get(url)) == accepted.json()["marker"]


def test_same_value_put_also_defeats_old_delivery(server):
    """Writing: an accepted same-value write defeats an older in-flight delivery."""
    reg, sink, url = attach(server)
    initial, _, _ = deliver(server, reg, sink)
    before = marker(get(url))
    assert put(url, b"1").status_code == 200
    assert marker(get(url)) == before
    conflict, _, _ = deliver(server, reg, sink, 2, expected=initial.fingerprint, seq=2)
    assert conflict.outcome == "conflict" and get(url).json() == 1


def test_served_checksum_owns_a_claim_until_replaced_or_detached(server):
    """Record: a record owns its checksum after the delivery lease is released."""
    from seamless.caching.buffer_cache import get_buffer_cache
    cache = get_buffer_cache()
    first = Buffer("claim-first-transport", "text").get_checksum()
    second = Buffer("claim-second-transport", "text").get_checksum()
    def count(checksum):
        return cache.reference_snapshot().get(checksum, (0, 0, False))[0]
    first_baseline, second_baseline = count(first), count(second)
    reg, sink, url = attach(server, celltype="text")
    ack, checksum, _ = deliver(server, reg, sink, "claim-first-transport")
    assert checksum == first.hex() and count(first) > first_baseline
    deliver(server, reg, sink, "claim-second-transport", expected=ack.fingerprint, seq=2)
    assert count(first) == first_baseline and count(second) > second_baseline
    server.unregister(reg).result(10)
    assert count(second) == second_baseline and get(url).status_code == 404


def test_marker_lifetime_and_registration_sets(server):
    """Record; Websocket; Lifecycle: remove snapshot, same URL continues markers."""
    owner = object()
    reg, sink, url = attach(server, owner=owner)
    deliver(server, reg, sink)
    before = marker(get(url))
    with updates(server.url + "/ctx") as receive:
        receive("shares")
        server.unregister(reg).result(10)
        assert "a" not in receive("shares")[1]
        assert get(url).status_code == 404
        reg2, sink2, same = attach(server, session_id="new", owner=owner)
        deliver(server, reg2, sink2, 2)
        assert same == url and marker(get(same)) > before
        snapshot = receive("shares")[1]
        assert "a" in snapshot


def test_namespace_websocket_includes_toplevel_changes(server):
    """Websocket: namespace snapshot and updates include its top-level shares."""
    reg, sink, url = attach(server, Spec("top", toplevel=True))
    initial, checksum, _ = deliver(server, reg, sink)
    assert initial.outcome == "written"
    sink.drain()
    with updates(server.url + "/ctx") as receive:
        assert receive() == ["Seamless share update server", "1.0"]
        snapshot = receive("shares")[1]["top"]
        assert snapshot["url"] == "/top" and snapshot["checksum"] == checksum
        accepted = put(url, b"7")
        assert accepted.status_code == 200
        assert receive("update")[1] == ["top", accepted.json()["checksum"], accepted.json()["marker"]]
        observed = sink.receive("_mount_observed")
        delivered, checksum, _ = deliver(server, reg, sink, 8, expected=observed.fingerprint, seq=2)
        assert delivered.outcome == "written"
        assert receive("update")[1] == ["top", checksum, marker(get(url))]
        server.unregister(reg).result(10)
        assert "top" not in receive("shares")[1]
        assert get(url).status_code == 404


def test_namespace_close_removes_toplevel_url(server):
    """Lifecycle; Websocket: Context namespace close removes top-level shares."""
    reg, sink, url = attach(server, Spec("top", toplevel=True))
    deliver(server, reg, sink)
    with updates(server.url + "/ctx") as receive:
        receive("shares")
        server.unregister(reg, close_namespace=True).result(10)
        assert receive("closed")[1] == 1001
    assert get(url).status_code == 404


def test_404_json_distinguishes_absent_share_and_no_value(server):
    """Reading: 404 JSON distinguishes a missing URL from an empty record."""
    reg, sink, url = attach(server)
    empty = get(url)
    missing = get(server.url + "/ctx/missing")
    assert empty.status_code == missing.status_code == 404
    assert empty.headers["Content-Type"].startswith("application/json")
    assert missing.headers["Content-Type"].startswith("application/json")
    assert empty.json() != missing.json()


def test_reservation_validation_is_atomic(server):
    """Errors; Namespaces: invalid MIME/key reserves neither URL nor namespace."""
    for spec in (Spec("a", mimetype="not a MIME type"), Spec("a/b", toplevel=True)):
        with pytest.raises(ValueError):
            server.reserve("invalid", spec, "int", "invalid", Sink(), owner=object())
        owner = object()
        reg, sink, url = attach(server, namespace="invalid", owner=owner)
        server.unregister(reg, close_namespace=True).result(10)
    for namespace in ("openapi.json", "seamless-client.js"):
        with pytest.raises(ValueError):
            server.reserve(namespace, Spec(), "int", namespace, Sink())


@pytest.mark.parametrize("first_top", [False, True])
def test_namespace_key_cannot_name_both_normal_and_toplevel_share(server, first_top):
    """Errors; Websocket: one namespace key cannot represent two share URLs."""
    owner = object()
    reg, sink, url = attach(server, Spec("same", toplevel=first_top), owner=owner)
    deliver(server, reg, sink)
    with pytest.raises(ValueError, match="in use"):
        server.reserve("ctx", Spec("same", toplevel=not first_top), "int", "duplicate-key", Sink(), owner=owner)
    assert get(url).json() == 1
    alternate = server.url + ("/ctx/same" if first_top else "/same")
    assert get(alternate).status_code == 404
    with updates(server.url + "/ctx") as receive:
        assert set(receive("shares")[1]) == {"same"}


def test_namespace_cannot_claim_existing_toplevel_name_via_toplevel_reservation(server):
    """Namespaces; URL space: claimed top-level key cannot become a namespace."""
    reg, sink, url = attach(server, Spec("beta", toplevel=True), namespace="alpha")
    deliver(server, reg, sink)
    with pytest.raises(ValueError):
        server.reserve("beta", Spec("other", toplevel=True), "int", "collision", Sink(), owner=object())
    assert get(server.url + "/other").status_code == 404
    server.unregister(reg, close_namespace=True).result(10)
    # The failed reservation must not have left an owner claim behind.
    accepted, accepted_sink, accepted_url = attach(server, namespace="beta", owner=object())
    deliver(server, accepted, accepted_sink, 2)
    assert get(accepted_url).json() == 2


def test_namespaces_and_url_reservations(server):
    """Namespaces; URL space: ownership and top-level reserved-key collisions."""
    owner = object()
    reg, sink, url = attach(server, owner=owner)
    attach(server, Spec("b"), session_id="b", owner=owner)
    with pytest.raises(ValueError, match="namespace.*in use"):
        server.reserve("ctx", Spec("c"), "int", "c", Sink(), owner=object())
    with pytest.raises(ValueError, match="in use"):
        server.reserve("ctx", Spec("a"), "int", "duplicate", Sink(), owner=owner)
    for path in ("openapi.json", "seamless-client.js", "ctx"):
        with pytest.raises(ValueError):
            server.reserve("other", Spec(path, toplevel=True), "int", path, Sink())
    attach(server, Spec("top", toplevel=True), namespace="other", session_id="top")
    with pytest.raises(ValueError):
        server.reserve("top", Spec("a"), "int", "namespace", Sink())


def test_readonly_cors_and_redirect(server):
    """URL space; Server; Writing: read-only Allow, wildcard CORS, redirects."""
    reg, sink, url = attach(server, Spec("index.html", readonly=True), "text")
    deliver(server, reg, sink, "<html/>")
    response = put(url, b"new")
    assert response.status_code == 405
    assert {s.strip() for s in response.headers["Allow"].split(",")} == {"GET", "HEAD"}
    response = get(server.url + "/ctx/", allow_redirects=False)
    assert response.status_code == 302 and response.headers["Location"] == "/ctx/index.html"
    response = get(url, headers={"Origin": "https://example.org"})
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    exposed = response.headers["Access-Control-Expose-Headers"].lower()
    assert "etag" in exposed and "x-seamless-marker" in exposed
    preflight = requests.options(server.url + "/anything", timeout=10, headers={"Origin": "https://example.org", "Access-Control-Request-Method": "PUT"})
    assert preflight.status_code in (200, 204)
    assert preflight.headers["Access-Control-Allow-Origin"] == "*"


def test_body_limit_and_put_wait_limit():
    """Limits; Writing: body over limit is 413; delayed installation is 503."""
    from seamless_workflow.attachments.share.server import ShareServer
    server = ShareServer(host="127.0.0.1", port=0, max_body_size=16, put_timeout=0.1)
    pending = None
    try:
        reg, sink, url = attach(server, celltype="text")
        sink.drain()
        assert put(url, b"x" * 17).status_code == 413
        assert sink.observed.empty()
        sink.held = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(put, url, b"hello")
            observation, pending = sink.observed.get(timeout=10)
            assert response.result(timeout=10).status_code == 503
            pending.set_result(None)
            pending = None
    finally:
        if pending is not None and not pending.done():
            pending.set_result(None)
        server.close()


def test_cut_waits_for_earlier_accepted_put(server):
    """Barrier: a cut includes processing of every previously accepted PUT."""
    reg, sink, url = attach(server)
    sink.drain()
    sink.held = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(put, url, b"6")
        observation, installed = sink.observed.get(timeout=10)
        cut = server.request_cut(reg, "cut-1")
        try:
            assert not cut.done()
        finally:
            installed.set_result(None)
        assert request.result(timeout=10).status_code == 200
        cut.result(10)
        session_id, cut_id, ws = sink.receive("_mount_cut")
        assert session_id == reg.session_id and cut_id == "cut-1"
        assert ws >= observation.ws


def test_get_resolution_wait_limit():
    """Limits; Reading: stalled resolution returns 503 while HEAD remains usable."""
    from seamless_workflow.attachments.share.server import ShareServer
    entered, release = threading.Event(), threading.Event()
    def resolve(checksum):
        entered.set()
        assert release.wait(10)
        return b"1"
    server = ShareServer(host="127.0.0.1", port=0, get_timeout=0.1, resolver=resolve)
    try:
        reg, sink, url = attach(server)
        deliver(server, reg, sink)
        response = get(url)
        assert entered.is_set() and response.status_code == 503
        assert requests.head(url, timeout=2).status_code == 200
    finally:
        release.set()
        server.close()


def test_unregister_rejects_new_put_and_namespace_closes_websocket(server):
    """Lifecycle; Websocket: final namespace detach closes updates with 1001."""
    reg, sink, url = attach(server)
    with updates(server.url + "/ctx") as receive:
        receive("shares")
        server.unregister(reg, close_namespace=True).result(10)
        assert receive("closed")[1] == 1001
    assert put(url, b"2").status_code == 404
    assert get(url).status_code == 404


def test_resolution_uses_private_worker_threads(server):
    """Server: buffer work uses private executor rather than caller/server loop."""
    worker_ids = []
    main = threading.get_ident()
    def resolve(checksum):
        worker_ids.append((threading.get_ident(), threading.current_thread().name))
        return b"1"
    server.resolver = resolve
    reg, sink, url = attach(server)
    deliver(server, reg, sink)
    assert get(url).content == b"1"
    assert worker_ids and worker_ids[0][0] != main
    assert not worker_ids[0][1].startswith("asyncio_"), "default asyncio executor used"
    # A blocked resolver must leave the server loop free to answer HEAD.
    entered = threading.Event()
    release = threading.Event()
    def blocked(checksum):
        entered.set()
        assert release.wait(10)
        return b"1"
    server.resolver = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(get, url)
        assert entered.wait(10)
        try:
            assert requests.head(url, timeout=2).status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 200


def test_accepted_put_is_in_cut_before_websocket_broadcast_finishes(server, monkeypatch):
    """Barrier; Writing: acceptance enters the cut before any broadcast await."""
    reg, sink, url = attach(server)
    deliver(server, reg, sink)
    sink.drain()
    sink.held = True
    entered, release = threading.Event(), threading.Event()
    original = server._broadcast_update

    async def held_broadcast(registration):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return await original(registration)

    monkeypatch.setattr(server, "_broadcast_update", held_broadcast)
    pending_install = None
    cut = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(put, url, b"9")
        try:
            assert entered.wait(10), "PUT never reached acceptance broadcast"
            assert sink.observed.empty(), "cell observation preceded websocket notification"
            cut = server.request_cut(reg, "acceptance-race")
            # A controller operation after the cut has been scheduled makes the
            # ordering deterministic without waiting on wall-clock delivery.
            server.poll(reg).result(10)
            assert not cut.done(), "cut overtook an already accepted PUT"
            assert sink.observed.empty(), "cell observation preceded websocket notification"
            assert all(op != "_mount_cut" for op, _ in list(sink.events.queue))
            release.set()
            observation, pending_install = sink.observed.get(timeout=10)
            assert not cut.done(), "cut reported before installation"
            pending_install.set_result(None)
            pending_install = None
            assert request.result(timeout=10).status_code == 200
            cut.result(10)
            assert sink.receive("_mount_cut")[1] == "acceptance-race"
        finally:
            sink.held = False
            release.set()
            if pending_install is not None and not pending_install.done():
                pending_install.set_result(None)
            # Also release observations queued after a failed assertion.
            while not sink.observed.empty():
                _, reply = sink.observed.get_nowait()
                if not reply.done():
                    reply.set_result(None)


def test_concurrent_start_has_one_listener_and_thread(monkeypatch):
    """Server: concurrent lazy starts create exactly one listening thread."""
    from seamless_workflow.attachments.share.server import ShareServer
    service = ShareServer(host="127.0.0.1", port=0)
    original = service._thread_main
    launched = queue.Queue()
    release = threading.Event()
    barrier = threading.Barrier(8)

    def paused_thread_main():
        launched.put(threading.get_ident())
        assert release.wait(10)
        return original()

    monkeypatch.setattr(service, "_thread_main", paused_thread_main)
    def start():
        barrier.wait(timeout=10)
        return service.start()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = [pool.submit(start) for _ in range(8)]
            first = launched.get(timeout=10)
            # Give competing callers a bounded window while startup is paused.
            import time
            time.sleep(0.05)
            release.set()
            assert all(task.result(timeout=15) is service for task in tasks)
            assert launched.empty(), "more than one server thread was launched"
            assert service.port > 0
            assert get(service.url + "/missing").status_code == 404
    finally:
        release.set()
        service.close()


def test_wildcard_url_uses_localhost_and_actual_ephemeral_port():
    """The API; Server: wildcard listener advertises localhost and bound port."""
    from seamless_workflow.attachments.share.server import ShareServer
    service = ShareServer(host="0.0.0.0", port=0)
    try:
        service.start()
        assert service.port > 0
        assert service.url == "http://localhost:" + str(service.port)
        assert get(service.url + "/missing").status_code == 404
    finally:
        service.close()
