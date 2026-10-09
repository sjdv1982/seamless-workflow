"""HTTP shares contract: real HTTP plus a websocket reader on its own thread.

Phase selectors: TestShareAPI (step 4), TestSharePersistence (step 5),
TestShareOpenAPI (step 6), TestShareClientAsset (step 7). No skips or xfails:
missing implementation is deliberately RED. One pytest process for this file.
"""
import asyncio
import hashlib
import json
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager

import aiohttp
import pytest
import requests

from seamless import Buffer, Cell, Checksum
from seamless.checksum.null import NULL_CHECKSUM
from seamless_workflow import Context
from seamless_workflow.errors import AuthorityError, PathError


@pytest.fixture(scope="module")
def server():
    # Deferred so absence produces setup failures after successful collection.
    from seamless_workflow import shareserver
    shareserver.configure(host="127.0.0.1", port=0)
    yield shareserver
    shareserver.close()


@pytest.fixture
def ctx(server):
    with Context() as context:
        yield context


def cell(ctx, value=1, celltype="int", name="a"):
    setattr(ctx, name, Cell(celltype=celltype))
    getattr(ctx, name).set(value)
    return getattr(ctx, name)


def get(url, **kwargs):
    return requests.get(url, timeout=10, **kwargs)


def put(url, body, **kwargs):
    return requests.put(url, data=body, timeout=10, **kwargs)


def marker(response):
    return int(response.headers["X-Seamless-Marker"])


def identity(x):
    return x


def fail_negative(x):
    if x < 0:
        raise ValueError("negative input")
    return x


@contextmanager
def updates(url):
    """Instrument: keep the websocket open independently of blocking test code."""
    inbox = queue.Queue()
    ready = threading.Event()
    stop = threading.Event()

    async def reader():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url.replace("http://", "ws://")) as ws:
                    ready.set()
                    while not stop.is_set():
                        try:
                            message = await ws.receive(timeout=0.1)
                        except asyncio.TimeoutError:
                            continue
                        if message.type == aiohttp.WSMsgType.TEXT:
                            inbox.put(json.loads(message.data))
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                            inbox.put(("closed", ws.close_code))
                            return
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise ws.exception()
        except BaseException as exc:
            inbox.put(exc)
            ready.set()

    thread = threading.Thread(target=lambda: asyncio.run(reader()), daemon=True)
    thread.start()
    assert ready.wait(10), "websocket failed to connect"

    def receive(kind=None):
        while True:
            value = inbox.get(timeout=10)
            if isinstance(value, BaseException):
                raise value
            if kind is None or value[0] == kind:
                return value

    try:
        yield receive
    finally:
        stop.set()
        thread.join(10)
        assert not thread.is_alive(), "websocket reader did not stop"


class TestShareAPI:
    def test_defaults_and_busy_port_refusal(self):
        """Server; Errors: bind defaults and busy-port failure install no slot."""
        script = '''
import os
import socket
from seamless import Cell
from seamless_workflow import Context, shareserver
assert shareserver.host == "0.0.0.0" and shareserver.port == 5813
with socket.socket() as occupied:
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    shareserver.configure(host="127.0.0.1", port=occupied.getsockname()[1])
    with Context() as ctx:
        ctx.a = Cell(celltype="int")
        ctx.a.set(1)
        try:
            ctx.a.share()
        except OSError:
            assert ctx.a.share.spec is None
        else:
            raise AssertionError("busy port must raise OSError")
        shareserver.configure(host="127.0.0.1", port=0)
        ctx.a.share()
        assert ctx.a.share.spec is not None
'''
        import os
        env = dict(os.environ)
        env.pop("SEAMLESS_SHARE_HOST", None)
        env.pop("SEAMLESS_SHARE_PORT", None)
        result = subprocess.run([sys.executable, "-c", script], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_property_arbitration_and_empty_handle(self, ctx):
        """The API; Lifecycle: class property, item escape and empty handle."""
        assert isinstance(Cell.share, property) and Cell.share.fdel is not None
        cell(ctx, {"share": 17}, "plain")
        assert ctx.a["share"].value == 17
        for field in ("spec", "url", "status", "error"):
            assert getattr(ctx.a.share, field) is None
        del ctx.a.share
        assert ctx.a.value == {"share": 17}
        with pytest.raises(AttributeError, match="shares is reserved"):
            ctx.shares = 3

    def test_share_blocks_through_initial_delivery_and_status(self, ctx, server):
        """The API; The initial decision; Server: endpoint ready on return."""
        cell(ctx)
        assert ctx.a.share() is None
        assert server.port > 0 and server.url.startswith("http://")
        assert ctx.a.share.url == server.url + "/ctx/a"
        response = get(ctx.a.share.url)
        assert response.status_code == 200 and json.loads(response.content) == 1
        spec = ctx.a.share.spec
        assert spec.driver == "share" and spec.mode == "w" and spec.authority == "cell"
        assert ctx.a.share.status["disk_checksum"] == ctx.a.checksum.hex()
        assert ctx.a.share.error is None
        ctx.a.share.clear_error()
        with pytest.raises(RuntimeError):
            server.configure(port=0)

    @pytest.mark.parametrize("readonly", [True, False])
    def test_initial_empty_cell(self, ctx, readonly):
        """The initial decision: empty read-only absent; writable installs null."""
        ctx.a = Cell(celltype="int")
        ctx.a.share(readonly=readonly)
        response = get(ctx.a.share.url)
        assert response.status_code == (404 if readonly else 204)
        if not readonly:
            assert ctx.a.checksum == Checksum(NULL_CHECKSUM)
            assert response.content == b""

    def test_get_head_checksum_etag_and_no_subpaths(self, ctx):
        """Reading: byte-for-byte body, HEAD metadata, checksum mode, ETag."""
        cell(ctx, {"a": 1}, "plain").share()
        url = ctx.a.share.url
        response = get(url)
        checksum = hashlib.sha256(response.content).hexdigest()
        assert response.headers["ETag"] == '"' + checksum + '"'
        assert checksum == ctx.a.checksum.hex()
        assert response.headers["Cache-Control"] == "no-cache"
        head = requests.head(url, timeout=10)
        assert head.status_code == 200 and not head.content
        assert head.headers["ETag"] == response.headers["ETag"]
        assert marker(head) == marker(response)
        conditional = get(url, headers={"If-None-Match": response.headers["ETag"]})
        assert conditional.status_code == 304 and not conditional.content
        assert get(url, params={"mode": "checksum"}).text == checksum
        assert get(url + "/a").status_code == 404

    @pytest.mark.parametrize("celltype,value,path,mimetype,expected", [
        ("text", "hello", "index.html", None, "text/html"),
        ("bytes", b"abc", "logo.png", None, "image/png"),
        ("str", "hello", "a", None, "application/json"),
        ("python", "this is not syntax!", "a", None, "text/x-python"),
        ("yaml", "a: 1", "a", None, "application/yaml"),
        ("text", "hello", "a", "application/xml", "application/xml"),
    ])
    def test_content_types(self, ctx, celltype, value, path, mimetype, expected):
        """Content types: explicit override, extension and celltype defaults."""
        cell(ctx, value, celltype).share(path, mimetype=mimetype)
        response = get(ctx.a.share.url)
        assert response.headers["Content-Type"].split(";")[0] == expected
        if expected.startswith("text/"):
            assert "charset=utf-8" in response.headers["Content-Type"].lower()
        if celltype == "str":
            assert json.loads(response.content) == "hello" and response.content.startswith(b'"')

    def test_put_canonicalization_cas_and_same_value(self, ctx):
        """Writing; Record: installed before 200; base-marker CAS; idempotence."""
        cell(ctx, {"a": 0}, "plain").share(readonly=False)
        url = ctx.a.share.url
        before = get(url)
        response = put(url, b'{"a":1}', params={"marker": marker(before)})
        assert response.status_code == 200
        record = response.json()
        assert ctx.a.value == {"a": 1}
        assert ctx.a.checksum.hex() == record["checksum"]
        assert record["marker"] == marker(before) + 1
        after = get(url)
        assert after.content == ctx.a.buffer.content
        repeat = put(url, after.content)
        assert repeat.status_code == 200 and repeat.json() == record
        stale = put(url, b'{"a":2}', params={"marker": marker(before)})
        assert stale.status_code == 409 and stale.json() == record
        assert ctx.a.value == {"a": 1}
        assert put(url, b'{"a":3}').status_code == 200
        ctx.compute()
        assert ctx.a.value == {"a": 3}

    @pytest.mark.parametrize("body", [b"", b"null"])
    def test_null_put(self, ctx, body):
        """Writing; Reading: empty and null bodies install the null checksum."""
        cell(ctx).share(readonly=False)
        assert put(ctx.a.share.url, body).status_code == 200
        assert ctx.a.checksum == Checksum(NULL_CHECKSUM)
        response = get(ctx.a.share.url)
        assert response.status_code == 204 and response.content == b""
        assert response.headers["ETag"] == '"' + ctx.a.checksum.hex() + '"'

    def test_invalid_put_changes_nothing(self, ctx):
        """Writing; Errors: malformed input never becomes a sense error."""
        cell(ctx).share(readonly=False)
        url = ctx.a.share.url
        before = get(url)
        for body in (b"not json", b'"not an integer"', b"{}"): 
            response = put(url, body)
            assert response.status_code == 422 and response.json()["error"]
            assert ctx.a.value == 1 and ctx.a.state == "complete"
            assert ctx.a.share.error is None
            assert marker(get(url)) == marker(before)
        assert put(url, b"2", params={"marker": "bad"}).status_code == 400
        assert put(url, b"2", params={"mode": "bad"}).status_code == 400
        assert ctx.a.value == 1

    def test_readonly_and_code_without_syntax_check(self, ctx):
        """Writing: read-only Allow header; code input has no syntax validation."""
        cell(ctx).share()
        response = put(ctx.a.share.url, b"2")
        assert response.status_code == 405
        assert {x.strip() for x in response.headers["Allow"].split(",")} == {"GET", "HEAD"}
        cell(ctx, "pass", "python", "code").share(readonly=False)
        assert put(ctx.code.share.url, b"not valid python @@@\n").status_code == 200
        assert ctx.code.buffer.content == b"not valid python @@@\n"
        assert get(ctx.code.share.url).content == ctx.code.buffer.content
        assert ctx.code.state == "complete" and ctx.code.exception is None
        assert ctx.code.share.error is None
        # The share follows ordinary assignment validation. The parser's
        # exception class belongs to core; pin parity without changing it here.
        cell(ctx, "not valid python @@@\n", "python", "assigned_code")
        with pytest.raises(Exception) as assigned_error:
            ctx.assigned_code.value
        with pytest.raises(type(assigned_error.value)):
            ctx.code.value
        assert ctx.code.state == "complete" and ctx.code.exception is None

    def test_namespaces_paths_toplevel_and_redirects(self, ctx, server):
        """Namespaces; URL space: runtime namespace, nested key, redirect."""
        ctx.shares.namespace = "one"
        cell(ctx, "<html/>", "text").share("index.html")
        response = get(server.url + "/one/", allow_redirects=False)
        assert response.status_code == 302 and response.headers["Location"] == "/one/index.html"
        with pytest.raises(ValueError):
            ctx.shares.namespace = "two"
        with Context() as other:
            cell(other)
            other.shares.namespace = "one"
            with pytest.raises(ValueError, match="namespace.*in use"):
                other.a.share()
            other.shares.namespace = "two"
            other.a.share("nested/value")
            assert get(other.a.share.url).status_code == 200
            del other.a.share
            other.a.share("index.html", toplevel=True)
            response = get(server.url + "/", allow_redirects=False)
            assert response.status_code == 302 and response.headers["Location"] == "/index.html"

    @pytest.mark.parametrize("path", ["", "/a", "a/", "a//b", ".", "..", "a/../b", "a?b", "a#b", "a\n"])
    def test_invalid_keys(self, ctx, path):
        """The API; Errors: invalid path segments fail without installing a slot."""
        cell(ctx)
        with pytest.raises(ValueError):
            ctx.a.share(path)
        assert ctx.a.share.spec is None

    def test_collision_and_reserved_keys(self, ctx):
        """URL space; Errors: occupied slots/URLs and reserved top-level paths."""
        cell(ctx).share()
        with pytest.raises(ValueError, match="already shared"):
            ctx.a.share()
        cell(ctx, 2, name="b")
        with pytest.raises(ValueError, match="in use"):
            ctx.b.share("a")
        for key in ("openapi.json", "seamless-client.js", "ctx"):
            with pytest.raises(ValueError):
                ctx.b.share(key, toplevel=True)
        with pytest.raises(ValueError):
            ctx.b.share("two/segments", toplevel=True)
        with pytest.raises(ValueError):
            ctx.b.share(mimetype="not a mime type")

    @pytest.mark.parametrize("celltype", ["folder", "deepfolder", "deepcell", "checksum", "module"])
    def test_unshareable_celltypes(self, ctx, celltype):
        """Shareable celltypes; Errors: rejected types have no share slot."""
        ctx.a = Cell(celltype=celltype)
        with pytest.raises(TypeError, match="not shareable"):
            ctx.a.share()
        assert ctx.a.share.spec is None

    def test_scope_and_topology_refusals(self, ctx):
        """Errors; Share slot: bound whole cells only; writable producer rules."""
        with pytest.raises(AttributeError, match="bound workflow cells"):
            Cell(celltype="int").share()
        cell(ctx, {"x": 1}, "plain")
        with pytest.raises(AttributeError, match="whole Context cell"):
            ctx.a.x.share()
        cell(ctx, 2, name="source")
        ctx.out = ctx.source
        ctx.compute()
        with pytest.raises(AuthorityError):
            ctx.out.share(readonly=False)
        ctx.out.share()
        assert get(ctx.out.share.url).status_code == 200
        cell(ctx, 3, name="target").share(readonly=False)
        with pytest.raises(AuthorityError, match="^Sensing mount is the producer; unmount first$"):
            ctx.target = ctx.source
        with pytest.raises(AuthorityError):
            ctx.target.checksum = None
        with pytest.raises((TypeError, AuthorityError, ValueError)):
            ctx.target.celltype = "text"

    def test_transformer_result_requires_connected_cell(self, ctx):
        """Errors: read-only transformer result refused; connect an output cell."""
        cell(ctx)
        ctx.tf = identity
        ctx.tf.pins.x = ctx.a
        ctx.compute()
        with pytest.raises(AttributeError, match="whole Context cell"):
            ctx.tf.result.share()
        ctx.out = ctx.tf.result
        ctx.out.share()
        assert get(ctx.out.share.url).json() == 1

    def test_head_does_not_resolve_and_get_failure_is_503(self, ctx, monkeypatch):
        """Reading; Record: HEAD uses checksum only; unavailable bytes give 503."""
        cell(ctx).share()
        url = ctx.a.share.url
        before = get(url)
        target = ctx.a.checksum.hex()
        original = Checksum.resolve
        original_resolution = Checksum.resolution

        def unavailable(checksum, *args, **kwargs):
            if checksum.hex() == target:
                raise RuntimeError("test instrument: bytes temporarily unavailable")
            return original(checksum, *args, **kwargs)

        async def unavailable_async(checksum, *args, **kwargs):
            if checksum.hex() == target:
                raise RuntimeError("test instrument: bytes temporarily unavailable")
            return await original_resolution(checksum, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Checksum, "resolve", unavailable)
            patch.setattr(Checksum, "resolution", unavailable_async)
            head = requests.head(url, timeout=10)
            assert head.status_code == 200 and marker(head) == marker(before)
            assert get(url, params={"mode": "checksum"}).text == target
            assert get(url).status_code == 503
        assert get(url).content == before.content

    def test_last_complete_record_survives_failed_and_blocked_cell(self, ctx):
        """Reading; Record: failed/blocked cells keep the last complete value."""
        cell(ctx)
        ctx.tf = fail_negative
        ctx.tf.pins.x = ctx.a
        ctx.out = ctx.tf.result
        ctx.compute()
        ctx.out.share()
        url = ctx.out.share.url
        before = get(url)
        ctx.a.set(-1)
        ctx.compute()
        assert ctx.tf.state == "failed" and ctx.out.state in ("blocked", "failed")
        ctx.mounts.sync(timeout=10)
        after = get(url)
        assert after.status_code == 200 and after.content == before.content
        assert marker(after) == marker(before)

    def test_slots_coexist_and_detach_independently(self, ctx, tmp_path):
        """Share slot; Lifecycle; Barrier: file/widget/share independence."""
        from seamless_workflow.jupyter import traitlet
        cell(ctx).mount(tmp_path / "a", mode="rw", authority="cell")
        hub = traitlet(ctx.a)
        ctx.a.share(readonly=False)
        report = ctx.mounts.sync(timeout=10)
        assert (("a",), "share") in report
        assert (("a",), "file") in report and (("a",), "widget") in report
        assert put(ctx.a.share.url, b"8").status_code == 200
        ctx.mounts.sync(timeout=10)
        assert (tmp_path / "a").read_text().strip() == "8"
        old = ctx.a.share.url
        del ctx.a.share
        assert get(old).status_code == 404
        assert ctx.a.mount.spec is not None and traitlet(ctx.a) is hub
        (tmp_path / "a").write_text("9\n")
        ctx.mounts.sync(timeout=10)
        assert ctx.a.value == 9

    def test_websocket_initial_state_updates_and_reshare_marker(self, ctx):
        """Websocket; Record; Lifecycle: immediate snapshot and monotonic markers."""
        cell(ctx).share(readonly=False)
        with updates(ctx.shares.url) as receive:
            assert receive() == ["Seamless share update server", "1.0"]
            snapshot = receive("shares")[1]["a"]
            assert snapshot["url"] == "/ctx/a" and snapshot["readonly"] is False
            assert snapshot["binary"] is False
            assert snapshot["content_type"].startswith("application/json")
            assert snapshot["checksum"] == ctx.a.checksum.hex()
            response = put(ctx.a.share.url, b"7")
            event = receive("update")[1]
            assert event == ["a", response.json()["checksum"], response.json()["marker"]]
            old = response.json()["marker"]
            del ctx.a.share
            assert "a" not in receive("shares")[1]
            ctx.a.set(8)
            ctx.a.share(readonly=False)
            snapshot = receive("shares")[1]["a"]
            assert snapshot["marker"] >= old
            served = get(ctx.a.share.url)
            assert served.json() == 8 and marker(served) > old
            if snapshot["checksum"] != ctx.a.checksum.hex():
                event = receive("update")[1]
                assert event == ["a", ctx.a.checksum.hex(), marker(served)]
            else:
                assert snapshot["marker"] == marker(served)

    def test_context_close_removes_urls_and_closes_websocket(self, ctx, server):
        """Lifecycle; Websocket: close removes URLs and sends close code 1001."""
        cell(ctx).share()
        url = ctx.a.share.url
        with updates(ctx.shares.url) as receive:
            receive("shares")
            ctx.close()
            assert receive("closed")[1] == 1001
        assert get(url).status_code == 404
        with Context() as other:
            cell(other).share()
            assert get(other.a.share.url).status_code == 200
        assert get(server.url + "/absent").status_code == 404

    def test_node_delete_and_graph_replace_detach(self, ctx):
        """Lifecycle: deleting a node and replacing its graph remove its URL."""
        cell(ctx).share()
        url = ctx.a.share.url
        del ctx.a
        assert get(url).status_code == 404
        cell(ctx).share()
        ctx.set_graph({"__seamless_workflow__": "0.5", "nodes": [], "connections": []})
        assert get(url).status_code == 404

    def test_cors(self, ctx, server):
        """Server; URL space: wildcard CORS, exposed marker and ETag headers."""
        cell(ctx).share()
        response = get(ctx.a.share.url, headers={"Origin": "https://example.org"})
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        exposed = response.headers["Access-Control-Expose-Headers"].lower()
        assert "etag" in exposed and "x-seamless-marker" in exposed
        response = requests.options(server.url + "/anything", timeout=10,
                                    headers={"Origin": "https://example.org", "Access-Control-Request-Method": "PUT"})
        assert response.status_code in (200, 204)
        assert response.headers["Access-Control-Allow-Origin"] == "*"

    def test_concurrent_conditional_writes_have_one_winner(self, ctx):
        """Writing; Record: concurrent writes with one base marker cannot both win."""
        from concurrent.futures import ThreadPoolExecutor
        cell(ctx).share(readonly=False)
        url = ctx.a.share.url
        base = marker(get(url))
        start = threading.Barrier(2)

        def write(value):
            start.wait(timeout=10)
            return put(url, str(value), params={"marker": base})

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(write, 2)
            second = pool.submit(write, 3)
            responses = [first.result(timeout=15), second.result(timeout=15)]
        assert sorted(r.status_code for r in responses) == [200, 409]
        winner = next(r for r in responses if r.status_code == 200).json()
        assert marker(get(url)) == base + 1
        assert ctx.a.checksum.hex() == winner["checksum"]

    def test_outbound_latest_and_no_put_echo(self, ctx):
        """Cell to share; Barrier: latest value delivered; sensed PUT not redelivered."""
        cell(ctx).share(readonly=False)
        with updates(ctx.shares.url) as receive:
            receive("shares")
            for value in range(2, 8):
                ctx.a.set(value)
            report = ctx.mounts.sync(timeout=10)
            assert report.in_sync and (("a",), "share") in report
            response = get(ctx.a.share.url)
            assert response.json() == 7
            before = marker(response)
            accepted = put(ctx.a.share.url, b"8")
            assert accepted.status_code == 200
            ctx.mounts.sync(timeout=10)
            after = get(ctx.a.share.url)
            assert after.json() == 8 and marker(after) == before + 1

    def test_file_edit_is_served_and_put_fans_out(self, ctx, tmp_path):
        """Share slot: sensing file edits and PUTs each win and fan out."""
        path = tmp_path / "value"
        cell(ctx).mount(path, mode="rw", authority="cell")
        ctx.a.share(readonly=False)
        ctx.mounts.sync(timeout=10)
        path.write_text("11\n")
        ctx.mounts.sync(timeout=10)
        assert get(ctx.a.share.url).json() == 11
        assert put(ctx.a.share.url, b"12").status_code == 200
        ctx.mounts.sync(timeout=10)
        assert ctx.a.value == 12 and path.read_text().strip() == "12"


class TestSharePersistence:
    @pytest.mark.parametrize("version", ["0.2", "0.3", "0.4", "0.5"])
    def test_legacy_graph_without_shares_loads(self, ctx, version):
        """Graph serialization: 0.2 through 0.5 remain loadable without shares."""
        cell(ctx, 5)
        graph = ctx.get_graph()
        graph["__seamless_workflow__"] = version
        ctx.set_graph(graph)
        assert ctx.a.value == 5 and ctx.a.share.spec is None

    @pytest.mark.parametrize("case", ["unshareable", "noncell", "incoming", "duplicate"])
    def test_invalid_graph_topology_even_when_stripping_shares(self, ctx, case):
        """Graph serialization: cell admission, incoming edges and URL uniqueness."""
        if case == "noncell":
            ctx.tf = identity
            graph = ctx.get_graph()
            node = next(n for n in graph["nodes"] if n["path"] == ["tf"])
        else:
            if case == "unshareable":
                ctx.a = Cell(celltype="folder")
            else:
                cell(ctx)
            if case == "incoming":
                ctx.b = ctx.a
            elif case == "duplicate":
                cell(ctx, 2, name="b")
            graph = ctx.get_graph()
            node = next(n for n in graph["nodes"] if n["path"] == (["b"] if case == "incoming" else ["a"]))
        graph["__seamless_workflow__"] = "0.6"
        spec = {"path": "same", "readonly": case != "incoming", "mimetype": None, "toplevel": False}
        node["share"] = dict(spec)
        if case == "duplicate":
            next(n for n in graph["nodes"] if n["path"] == ["b"])["share"] = dict(spec)
        for enabled in (False, True):
            with pytest.raises(PathError, match="Invalid share spec:"):
                ctx.set_graph(graph, shares=enabled)

    def test_roundtrip_and_opt_out(self, ctx, server):
        """Graph serialization: normalized share spec, format 0.6, runtime namespace."""
        cell(ctx, 4).share(readonly=False)
        graph = ctx.get_graph()
        assert graph["__seamless_workflow__"] == "0.6"
        spec = graph["nodes"][0]["share"]
        assert spec == {"path": "a", "readonly": False, "mimetype": None, "toplevel": False}
        assert "namespace" not in json.dumps(graph)
        with Context() as other:
            other.shares.namespace = "copy"
            other.set_graph(graph)
            assert get(other.a.share.url).json() == 4
            assert other.a.share.spec.readonly is False
            old = other.a.share.url
            other.set_graph(graph, shares=False)
            assert other.a.share.spec is None
            assert "share" not in other.get_graph()["nodes"][0]
            assert get(old).status_code == 404

    @pytest.mark.parametrize("version", ["0.2", "0.3", "0.4", "0.5"])
    def test_legacy_version_refuses_share_even_opt_out(self, ctx, version):
        """Graph serialization: share entries below 0.6 rejected before stripping."""
        cell(ctx)
        graph = ctx.get_graph()
        graph["__seamless_workflow__"] = version
        graph["nodes"][0]["share"] = {"path": "a", "readonly": True, "mimetype": None, "toplevel": False}
        for enabled in (False, True):
            with pytest.raises(PathError, match="share entries require workflow graph version 0.6"):
                ctx.set_graph(graph, shares=enabled)

    @pytest.mark.parametrize("patch", [{"unknown": 1}, {"path": "a//b"}, {"readonly": "yes"}, {"toplevel": True, "path": "a/b"}])
    def test_invalid_specs_validate_with_opt_out(self, ctx, patch):
        """Graph serialization: invalid specs are rejected even with shares=False."""
        cell(ctx)
        graph = ctx.get_graph()
        graph["__seamless_workflow__"] = "0.6"
        graph["nodes"][0]["share"] = {"path": "a", "readonly": True, "mimetype": None, "toplevel": False, **patch}
        with pytest.raises(PathError, match="Invalid share spec:"):
            ctx.set_graph(graph, shares=False)
        assert ctx.a.value == 1

    def test_collision_load_is_atomic(self, ctx):
        """Graph serialization: occupied namespace raises without graph replacement."""
        cell(ctx, 3).share()
        graph = ctx.get_graph()
        with Context() as other:
            cell(other, 99, name="keep")
            before = other.get_graph()
            with pytest.raises(ValueError, match="namespace.*in use"):
                other.set_graph(graph)
            assert other.get_graph() == before and other.keep.value == 99

    def test_reserved_context_node(self, ctx):
        """The API: graph node named shares is refused with PathError."""
        cell(ctx)
        graph = ctx.get_graph()
        graph["nodes"][0]["path"] = ["shares"]
        with pytest.raises(PathError, match="shares is a reserved Context API name"):
            ctx.set_graph(graph)


class TestShareOpenAPI:
    @pytest.mark.parametrize("celltype,value,schema", [("int", 1, {"type": "integer"}), ("float", 1.5, {"type": "number"}), ("bool", True, {"type": "boolean"}), ("str", "a", {"type": "string"}), ("plain", {}, {}), ("text", "a", {"type": "string"})])
    def test_live_document_schema_and_readonly(self, ctx, server, celltype, value, schema):
        """openapi.json: live OpenAPI 3.1, cell schema, operation extensions."""
        cell(ctx, value, celltype).share()
        document = get(server.url + "/openapi.json").json()
        assert document["openapi"].startswith("3.1")
        assert document == server.openapi() == ctx.shares.openapi()
        assert document["x-seamless-updates"] == {"ctx": "/ctx"}
        path = document["paths"]["/ctx/a"]
        assert "get" in path and "head" in path and "put" not in path
        operation = path["get"]
        assert operation["x-seamless-celltype"] == celltype
        assert operation["x-seamless-node"] == ["a"]
        content = operation["responses"]["200"]["content"]
        assert next(iter(content.values()))["schema"] == schema
        del ctx.a.share
        assert "/ctx/a" not in server.openapi()["paths"]

    def test_writable_statuses_scope_and_no_graph_leak(self, ctx, server):
        """openapi.json: status codes, Context restriction, unshared nodes absent."""
        cell(ctx).share(readonly=False)
        cell(ctx, 2, name="hidden")
        with Context() as other:
            other.shares.namespace = "other"
            cell(other).share()
            all_paths = server.openapi()["paths"]
            assert set(all_paths) == {"/ctx/a", "/other/a"}
            assert set(ctx.shares.openapi()["paths"]) == {"/ctx/a"}
            path = all_paths["/ctx/a"]
            assert set(path["put"]["responses"]) >= {"200", "400", "404", "409", "413", "422", "503"}
            assert set(path["get"]["responses"]) >= {"200", "204", "304", "404", "503"}


class TestShareClientAsset:
    def test_dependency_free_client_served_without_share(self, ctx, server):
        """seamless-client.js: built-in dependency-free route on the same server."""
        cell(ctx).share()
        del ctx.a.share
        response = get(server.url + "/seamless-client.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["Content-Type"]
        assert "connect_seamless" in response.text
        assert "import " not in response.text and "require(" not in response.text


class TestShareRuntime:
    def test_sensed_put_never_dispatches_echo_delivery(self, ctx):
        """Cell to share: a sensed PUT is installed without outbound delivery echo."""
        from seamless_workflow.diagnostics import record_attachments
        cell(ctx).share(readonly=False)
        ctx.mounts.sync(timeout=10)
        with record_attachments(ctx) as log:
            assert put(ctx.a.share.url, b"19").status_code == 200
            ctx.mounts.sync(timeout=10)
            assert ctx.a.value == 19
            entries = [e for e in log.entries() if dict(e[3]).get("driver") == "share"]
            assert any(e[2] == "observation" for e in entries)
            assert not any(e[2] == "dispatch" for e in entries)

    def test_delivery_pacing_coalesces_and_is_per_session(self, ctx, monkeypatch):
        """Cell to share: latest-only deliveries start at least 2/3 second apart."""
        import time
        from seamless_workflow.attachments.share.driver import ShareDriver
        calls = []
        original = ShareDriver.deliver
        def record(driver, registration, delivery):
            calls.append((registration.session_id, time.monotonic(), delivery.checksum))
            return original(driver, registration, delivery)
        monkeypatch.setattr(ShareDriver, "deliver", record)
        cell(ctx, 0).share()
        cell(ctx, 0, name="b").share()
        for value in range(1, 11):
            ctx.a.set(value)
        ctx.b.set(10)
        ctx.mounts.sync(timeout=10)
        grouped = {}
        for session, when, checksum in calls:
            grouped.setdefault(session, []).append((when, checksum))
        assert len(grouped) == 2
        for session, entries in grouped.items():
            assert len(entries) == 2
            assert entries[-1][1] == Buffer(10, "int").get_checksum().hex()
            assert entries[1][0] - entries[0][0] >= 2 / 3 - 0.005
        assert get(ctx.a.share.url).json() == get(ctx.b.share.url).json() == 10

    def test_public_calls_are_reentrant_refusals(self, ctx, monkeypatch):
        """Threads and blocking: public share calls refuse the controller thread."""
        from seamless_workflow.errors import ReentrantContextError
        cell(ctx)
        cell(ctx, 2, name="b").share()
        a, b, shares = ctx.a, ctx.b, ctx.shares
        observed = []
        original = Context._after_turn
        def check(context):
            if context is ctx and not observed:
                observed.append(True)
                for operation in (lambda: a.share(),
                                  lambda: b.share.unshare(),
                                  lambda: b.share.clear_error(),
                                  lambda: setattr(shares, "namespace", "other")):
                    with pytest.raises(ReentrantContextError):
                        operation()
            return original(context)
        monkeypatch.setattr(Context, "_after_turn", check)
        ctx.a.set(3)
        assert observed
        monkeypatch.setattr(Context, "_after_turn", original)

    def test_public_calls_after_close_refuse_closed_context(self, ctx):
        """Threads and blocking: public operations on held handles refuse close."""
        from seamless_workflow.errors import ClosedContextError
        cell(ctx).share()
        a, handle, shares = ctx.a, ctx.a.share, ctx.shares
        ctx.close()
        for operation in (lambda: a.share(), lambda: handle.unshare(),
                          lambda: handle.clear_error(),
                          lambda: setattr(shares, "namespace", "other")):
            with pytest.raises(ClosedContextError):
                operation()

    def test_missing_and_stale_handles_raise_node_error(self, ctx):
        """Errors: a missing or stale whole-node handle raises the share NodeError."""
        from seamless_workflow.errors import NodeError
        with pytest.raises(NodeError, match="Shares require an existing whole cell node"):
            ctx.missing.share()
        stale = cell(ctx)
        del ctx.a
        with pytest.raises(NodeError, match="Shares require an existing whole cell node"):
            stale.share()
        assert not ctx.mounts.sync(timeout=10)

    def test_unsharing_keeps_namespace_claim_until_context_close(self, ctx):
        """Namespaces; Lifecycle: live owner retains namespace through final unshare."""
        cell(ctx).share()
        del ctx.a.share
        with Context() as other:
            cell(other)
            with pytest.raises(ValueError, match="namespace.*in use"):
                other.a.share()
            ctx.close()
            other.a.share()
            assert get(other.a.share.url).json() == 1

    def test_namespace_can_change_after_final_unshare_without_leaking_old_claim(self, ctx):
        """Namespaces: namespace may change with no shares, releasing the old name."""
        cell(ctx).share()
        del ctx.a.share
        ctx.shares.namespace = "renamed"
        ctx.a.share()
        assert "/renamed/a" in ctx.a.share.url
        with Context() as other:
            cell(other).share()
            assert get(other.a.share.url).json() == 1

    def test_process_close_stops_listener(self):
        """Server; Lifecycle: seamless.close shuts down the process server."""
        script = '''
import requests
import seamless
from seamless import Cell
from seamless_workflow import Context, shareserver
shareserver.configure(host="127.0.0.1", port=0)
ctx = Context()
ctx.a = Cell(celltype="int")
ctx.a.set(1)
ctx.a.share()
url = ctx.a.share.url
assert requests.get(url, timeout=5).status_code == 200
seamless.close()
try:
    requests.get(url, timeout=1)
except requests.ConnectionError:
    pass
else:
    raise AssertionError("server still listens after seamless.close()")
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr


class TestShareRuntimeAudit:
    def test_same_namespace_assignment_after_close_checks_lifecycle(self, ctx):
        """Threads and blocking: same-value namespace setter still refuses close."""
        from seamless_workflow.errors import ClosedContextError
        shares = ctx.shares
        namespace = shares.namespace
        ctx.close()
        with pytest.raises(ClosedContextError):
            shares.namespace = namespace

    def test_shares_getter_reentrant_guard_precedes_close_lock(self, ctx, monkeypatch):
        """Threads and blocking: controller refuses shares before waiting on close."""
        from seamless_workflow.errors import ReentrantContextError
        cell(ctx)
        original = Context._after_turn
        visited = []
        def check(context):
            if context is ctx and not visited:
                visited.append(True)
                with pytest.raises(ReentrantContextError):
                    context.shares
            return original(context)
        monkeypatch.setattr(Context, "_after_turn", check)
        # Test instrument: mimic close holding its lock while awaiting a turn.
        # The bounded Future wait prevents a broken guard from hanging the test.
        with ctx._close_lock:
            future = ctx._controller.enqueue("_mount_status", (("a",),), {"driver": "share"}, klass=1)
            future.result(timeout=2)
        assert visited
        monkeypatch.setattr(Context, "_after_turn", original)

    def test_configuration_after_rejected_unstarted_reservation(self):
        """Server; Errors: a never-started failed reservation does not freeze config."""
        script = '''
from seamless import Cell
from seamless_workflow import Context, shareserver
shareserver.configure(host="127.0.0.1", port=0)
with Context() as ctx:
    ctx.a = Cell(celltype="int")
    ctx.a.set(1)
    try:
        ctx.a.share("openapi.json", toplevel=True)
    except ValueError:
        assert ctx.a.share.spec is None
    else:
        raise AssertionError("reserved route must be refused")
    shareserver.configure(host="127.0.0.1", port=0)
    ctx.a.share()
    assert shareserver.port > 0
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("readonly", [True, False])
    def test_complete_null_first_delivery(self, ctx, readonly):
        """Initial decision; Reading: complete null is served before share returns."""
        cell(ctx, None).share(readonly=readonly)
        response = get(ctx.a.share.url)
        assert response.status_code == 204 and not response.content
        assert ctx.a.checksum == Checksum(NULL_CHECKSUM)
        assert response.headers["ETag"] == '"' + ctx.a.checksum.hex() + '"'

    def test_readonly_empty_then_null(self, ctx):
        """Initial decision; Cell to share: no value followed by null becomes 204."""
        ctx.a = Cell(celltype="int")
        ctx.a.share()
        assert get(ctx.a.share.url).status_code == 404
        ctx.a.set(None)
        ctx.mounts.sync(timeout=10)
        assert get(ctx.a.share.url).status_code == 204

    def test_missing_handle_lifecycle_guards(self, ctx, monkeypatch):
        """Threads and blocking; Errors: MissingView shares obey public guards."""
        from seamless_workflow.errors import ClosedContextError, ReentrantContextError
        missing = ctx.missing
        cell(ctx)
        original = Context._after_turn
        visited = []
        def check(context):
            if context is ctx and not visited:
                visited.append(True)
                with pytest.raises(ReentrantContextError):
                    missing.share()
            return original(context)
        monkeypatch.setattr(Context, "_after_turn", check)
        ctx.a.set(2)
        assert visited
        monkeypatch.setattr(Context, "_after_turn", original)
        ctx.close()
        with pytest.raises(ClosedContextError):
            missing.share()

    def test_reserved_graph_node_is_phase_four_admission(self, ctx):
        """The API: shares is reserved in graph input before persistence is added."""
        cell(ctx)
        graph = ctx.get_graph()
        graph["nodes"][0]["path"] = ["shares"]
        with pytest.raises(PathError, match="^shares is a reserved Context API name$"):
            ctx.set_graph(graph)
        assert ctx.a.value == 1

    def test_failed_initial_delivery_rolls_back_only_failed_share(self, ctx, monkeypatch):
        """Lifecycle; Initial decision: failed attachment leaves sibling URLs intact."""
        from concurrent.futures import Future
        from seamless_workflow.attachments.share.driver import ShareDriver
        cell(ctx).share()
        sibling_url = ctx.a.share.url
        cell(ctx, 2, name="b")
        driver_server = ctx.shares._driver.server
        monkeypatch.setattr(driver_server, "put_timeout", 0.05)
        monkeypatch.setattr(driver_server, "delivery_timeout", 0.05)
        original = ShareDriver.deliver
        def hold(driver, registration, delivery):
            if registration.spec.path == "b":
                future = Future()
                future.set_result(None)
                return future  # Instrument an unacknowledged initial delivery.
            return original(driver, registration, delivery)
        with monkeypatch.context() as patch:
            patch.setattr(ShareDriver, "deliver", hold)
            with pytest.raises(TimeoutError):
                ctx.b.share()
        assert ctx.b.share.spec is None
        assert get(sibling_url).json() == 1
        assert get(sibling_url.rsplit("/", 1)[0] + "/b").status_code == 404
        assert set(ctx.mounts.sync(timeout=10)) == {(("a",), "share")}
        ctx.b.share()
        assert get(ctx.b.share.url).json() == 2

    @pytest.mark.parametrize("action", ["unshare", "delete"])
    def test_detach_uses_share_delivery_timeout_without_file_service(self, ctx, monkeypatch, action):
        """Lifecycle; Threads: share detach waits its transport delivery timeout."""
        from seamless_workflow.attachments.share.driver import ShareDriver
        from seamless_workflow.attachments.fs import service as file_service
        cell(ctx).share()
        share_server = ctx.shares._driver.server
        monkeypatch.setattr(share_server, "delivery_timeout", 1.75)
        monkeypatch.setattr(share_server, "put_timeout", 0.125)
        waits = []
        original = ShareDriver.unregister
        def track(driver, registration, **kwargs):
            future = original(driver, registration, **kwargs)
            original_result = future.result
            def result(timeout=None):
                waits.append(timeout)
                return original_result(timeout)
            future.result = result
            return future
        def no_file_service(*args, **kwargs):
            raise AssertionError("share detach initialized the file service")
        monkeypatch.setattr(ShareDriver, "unregister", track)
        monkeypatch.setattr(file_service, "get_service", no_file_service)
        url = ctx.a.share.url
        if action == "unshare":
            del ctx.a.share
        else:
            del ctx.a
        assert waits and waits[-1] == 1.75
        assert get(url).status_code == 404

    def test_namespace_change_races_share_as_one_public_operation(self, ctx):
        """Namespaces; Threads: namespace changes and share reservations serialize."""
        from concurrent.futures import ThreadPoolExecutor
        cell(ctx)
        handle = ctx.a
        barrier = threading.Barrier(2)
        def share():
            barrier.wait(timeout=10)
            handle.share()
        def rename():
            barrier.wait(timeout=10)
            try:
                ctx.shares.namespace = "renamed"
                return "renamed"
            except ValueError:
                return "ctx"
        with ThreadPoolExecutor(max_workers=2) as pool:
            sharing = pool.submit(share)
            renaming = pool.submit(rename)
            sharing.result(timeout=15)
            namespace = renaming.result(timeout=15)
        assert ctx.shares.namespace == namespace
        assert handle.share.url.endswith("/" + namespace + "/a")
        assert get(handle.share.url).json() == 1

    def test_environment_host_and_port_overrides(self):
        """Server: environment config overrides bind defaults before first share."""
        import os
        env = dict(os.environ, SEAMLESS_SHARE_HOST="127.0.0.1", SEAMLESS_SHARE_PORT="0")
        script = '''
from seamless import Cell
from seamless_workflow import Context, shareserver
assert shareserver.host == "127.0.0.1" and shareserver.port == 0
with Context() as ctx:
    ctx.a = Cell(celltype="int")
    ctx.a.set(1)
    ctx.a.share()
    assert shareserver.port > 0
    assert ctx.a.share.url == "http://127.0.0.1:" + str(shareserver.port) + "/ctx/a"
'''
        result = subprocess.run([sys.executable, "-c", script], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
