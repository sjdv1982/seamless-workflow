"""Share downloads contract (contracts/shares.md, *Downloads*): real HTTP.

One pytest process for this file.
"""
import io
import json
import tarfile
import threading
import time
import zipfile

import pytest
import requests

from seamless import Buffer, Cell
from seamless_workflow import Context
from seamless_workflow.errors import PathError

from test_contract_shares import updates


@pytest.fixture(scope="module")
def server():
    from seamless_workflow import shareserver
    shareserver.configure(host="127.0.0.1", port=0)
    yield shareserver
    shareserver.close()


@pytest.fixture
def short_hold_server(server):
    from seamless_workflow import shareserver
    shareserver.close()
    shareserver.configure(host="127.0.0.1", port=0, hold_timeout=1.0)
    yield shareserver
    shareserver.close()
    shareserver.configure(host="127.0.0.1", port=0)


@pytest.fixture
def ctx(server):
    with Context() as context:
        yield context


def get(url, **kwargs):
    return requests.get(url, timeout=10, **kwargs)


def buffer_of(ctx, path):
    """The cell's buffer: what a share GET (and so an archive member) serves."""
    node = ctx
    for part in path.split("/"):
        node = getattr(node, part)
    return node.buffer.content if hasattr(node.buffer, "content") else bytes(node.buffer)


def tar_members(content):
    with tarfile.open(fileobj=io.BytesIO(content)) as bundle:
        return {
            member.name: bundle.extractfile(member).read()
            for member in bundle.getmembers()
        }


def zip_members(content):
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        return {name: bundle.read(name) for name in bundle.namelist()}


def leaf(content):
    buffer = Buffer(content)
    buffer.tempref()
    return buffer.get_checksum().hex()


def fill(ctx):
    ctx.title = Cell(celltype="text")
    ctx.title.set("daily report")
    ctx.sub.table = Cell(celltype="plain")
    ctx.sub.table.set({"rows": [1, 2, 3]})
    ctx.raw = Cell(celltype="bytes")
    ctx.raw.set(b"\x00\xff")
    ctx.compute()


def identity(x):
    return x


def fail_negative(x):
    if x < 0:
        raise ValueError("negative input")
    return x


class TestDownloadGet:
    def test_members_are_buffers_and_headers(self, ctx):
        """*The download GET*: members byte for byte, headers, deterministic packing."""
        fill(ctx)
        url = ctx.shares.add_download(["title", "sub/table", "raw"])
        assert url == ctx.shares.url + "/download"
        response = get(url)
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/gzip"
        assert response.headers["Content-Disposition"] == 'attachment; filename="download.tar.gz"'
        assert response.headers["Cache-Control"] == "no-cache"
        members = tar_members(response.content)
        assert members == {
            path: buffer_of(ctx, path) for path in ("title", "sub/table", "raw")
        }
        with tarfile.open(fileobj=io.BytesIO(response.content)) as bundle:
            infos = bundle.getmembers()
        assert [info.name for info in infos] == sorted(members)
        assert all(info.mtime == 0 and info.uid == 0 and info.mode == 0o644 for info in infos)
        assert get(url).content == response.content

    @pytest.mark.parametrize("fmt, mimetype, reader", [
        ("tar", "application/x-tar", tar_members),
        ("tar.xz", "application/x-xz", tar_members),
        ("zip", "application/zip", zip_members),
    ])
    def test_formats(self, ctx, fmt, mimetype, reader):
        """*The download GET*: format fixes content type and file name."""
        fill(ctx)
        url = ctx.shares.add_download(["title"], format=fmt)
        response = get(url)
        assert response.headers["Content-Type"] == mimetype
        assert response.headers["Content-Disposition"].endswith(f'filename="download.{fmt}"')
        assert reader(response.content) == {"title": buffer_of(ctx, "title")}
        assert get(url).content == response.content

    def test_member_names_and_existing_extension(self, ctx):
        """*Downloads*: a dict names members; an existing extension is not repeated."""
        fill(ctx)
        url = ctx.shares.add_download(
            {"report/title.txt": "title", "data.json": "sub/table"},
            path="report.zip", format="zip",
        )
        response = get(url)
        assert response.headers["Content-Disposition"] == 'attachment; filename="report.zip"'
        assert zip_members(response.content) == {
            "report/title.txt": buffer_of(ctx, "title"),
            "data.json": buffer_of(ctx, "sub/table"),
        }

    def test_failures_are_listed_together(self, ctx):
        """*The download GET*: one 409 lists every failing path with its reason."""
        fill(ctx)
        ctx.n = Cell(celltype="int")
        ctx.n.set(-1)
        ctx.tf = fail_negative
        ctx.tf.pins.x = ctx.n
        ctx.out = ctx.tf.result
        ctx.empty = Cell(celltype="int")
        ctx.deep = Cell(celltype="deepcell")
        ctx.deep.set({})
        ctx.compute()
        url = ctx.shares.add_download(
            ["title", "nothing", "sub", "tf", "deep", "empty", "out"]
        )
        response = get(url)
        assert response.status_code == 409
        body = response.json()
        assert set(body) == {"error", "cells"}
        assert body["cells"] == {
            "nothing": "missing",
            "sub": "not-a-cell",
            "tf": "not-a-cell",
            "deep": "deep-celltype",
            "empty": "unwired",
            "out": ctx.out.state,
        }
        assert ctx.out.state in ("blocked", "failed")

    def test_null_cell_omitted_and_folder_expanded(self, ctx):
        """*The download GET*: null is left out; a folder becomes a directory of leaves."""
        fill(ctx)
        ctx.none = Cell(celltype="plain")
        ctx.none.set(None)
        ctx.files = Cell(celltype="folder")
        ctx.files.set({"a.txt": leaf(b"leaf a"), "deeper/b.bin": leaf(b"\x01\x02")})
        ctx.compute()
        url = ctx.shares.add_download(["title", "none", "files"])
        members = tar_members(get(url).content)
        assert members == {
            "title": buffer_of(ctx, "title"),
            "files/a.txt": b"leaf a",
            "files/deeper/b.bin": b"\x01\x02",
        }

    def test_registered_folder_download_does_not_retain_leaf_refs(
        self, ctx, monkeypatch
    ):
        import seamless.caching.buffer_cache as buffer_cache
        from seamless import CacheMissError, Checksum
        from seamless.caching.buffer_cache import get_buffer_cache
        from seamless.transformer import spawn
        from seamless_transformer.worker import shutdown_workers

        cache = get_buffer_cache()
        previous_interval = buffer_cache.eviction_interval_from_env()
        cache.stop_eviction_thread()
        monkeypatch.setattr(buffer_cache, "_TEMPREF_SCALE", 0.1)
        cache.start_eviction_thread(interval=0.1)

        def make_folder(value):
            return {"payload.txt": value.encode()}

        spawn(1)
        try:
            ctx.source = Cell(celltype="plain")
            ctx.folder_producer = make_folder
            ctx.folder_producer.local = True
            ctx.folder_producer.celltypes.value = "plain"
            ctx.folder_producer.celltypes.result = "folder"
            ctx.folder_producer.pins.value = ctx.source
            ctx.folder = ctx.folder_producer.result
            url = ctx.shares.add_download(
                ["folder"], path="folder.zip", format="zip"
            )

            payload = b"folder payload expires without a file mount"
            ctx.source.set(payload.decode())
            ctx.compute(timeout=30)
            root = ctx.folder.checksum
            manifest = json.loads(bytes(root.resolve().content))
            child = Checksum(manifest["payload.txt"])

            time.sleep(3.2)
            assert root.resolve()
            with pytest.raises(CacheMissError):
                child.resolve()
            response = get(url)
            assert response.status_code == 503
        finally:
            shutdown_workers()
            cache.stop_eviction_thread()
            cache.start_eviction_thread(interval=previous_interval)



def test_file_w_mount_supplies_evicted_folder_leaf_to_download(ctx, tmp_path):
    from seamless import Checksum
    from seamless.caching.buffer_cache import get_buffer_cache
    from seamless.transformer import spawn
    from seamless_transformer.worker import shutdown_workers

    spawn(1)
    try:
        ctx.source = Cell(celltype="plain")

        def make_folder(value):
            return {"payload.txt": value.encode()}

        ctx.folder_producer = make_folder
        ctx.folder_producer.local = True
        ctx.folder_producer.celltypes.value = "plain"
        ctx.folder_producer.celltypes.result = "folder"
        ctx.folder_producer.pins.value = ctx.source
        ctx.folder = ctx.folder_producer.result
        url = ctx.shares.add_download(
            ["folder"], path="folder-mounted.zip", format="zip"
        )

        payload = b"served from mounted file source"
        ctx.source.set(payload.decode())
        ctx.compute(timeout=30)
        root = ctx.folder.checksum
        manifest = json.loads(bytes(root.resolve().content))
        child = Checksum(manifest["payload.txt"])

        mount_path = tmp_path / "export-tree"
        ctx.folder.mount(mount_path, mode="w")
        ctx.mounts.sync(timeout=30)
        assert (mount_path / "payload.txt").read_bytes() == payload

        # Remove the in-memory copies after the write mount has populated its
        # local source. This prevents legacy write-echo leaf refholds from
        # being the reason the next download succeeds.
        cache = get_buffer_cache()
        with cache.lock:
            cache.strong_cache.pop(child, None)
            cache.weak_cache.pop(child, None)
        assert child not in cache.strong_cache
        assert child not in cache.weak_cache

        response = get(url)
        assert response.status_code == 200
        assert zip_members(response.content) == {"folder/payload.txt": payload}
    finally:
        shutdown_workers()



def test_download_waits_for_folder_leaf_recovery_and_ignores_superseded_result(
        ctx, monkeypatch
    ):
        import threading
        import seamless_workflow.context as context_module
        from seamless import Buffer, Checksum
        from seamless.caching.buffer_cache import get_buffer_cache

        cache = get_buffer_cache()
        payload_x = b"first folder leaf"
        payload_y = b"current folder leaf"
        child_buffer_x = Buffer(payload_x)
        child_buffer_y = Buffer(payload_y)
        root_buffer_x = Buffer(
            {"payload.txt": child_buffer_x.get_checksum().hex()}, "folder"
        )
        root_buffer_y = Buffer(
            {"payload.txt": child_buffer_y.get_checksum().hex()}, "folder"
        )
        root_x = Checksum(root_buffer_x.get_checksum())
        root_y = Checksum(root_buffer_y.get_checksum())
        child_x = Checksum(json.loads(bytes(root_buffer_x.content))["payload.txt"])
        child_y = Checksum(json.loads(bytes(root_buffer_y.content))["payload.txt"])

        def clear(checksum):
            with cache.lock:
                cache.strong_cache.pop(checksum, None)
                cache.weak_cache.pop(checksum, None)

        clear(child_x)
        clear(child_y)
        started_x, started_y = threading.Event(), threading.Event()
        release_x, release_y = threading.Event(), threading.Event()
        finished_x = threading.Event()

        def recover(checksum):
            if checksum == root_x:
                started_x.set()
                release_x.wait(10)
                Buffer(payload_x).tempref()
                finished_x.set()
            else:
                started_y.set()
                release_y.wait(10)
                Buffer(payload_y).tempref()
            return checksum

        monkeypatch.setattr(context_module, "_fingertip_folder_buffer", recover)
        try:
            ctx.folder = Cell(celltype="folder")
            url = ctx.shares.add_download(
                ["folder"], path="folder.zip", format="zip"
            )
            ctx.folder.set_checksum(root_x, input_celltype="folder")
            assert started_x.wait(10)
            assert ctx.folder.state == "waiting"
            response = get(url)
            assert response.status_code == 409
            assert response.json()["cells"] == {"folder": "waiting"}

            ctx.folder.set_checksum(root_y, input_celltype="folder")
            assert started_y.wait(10)
            release_x.set()
            assert finished_x.wait(10)
            assert ctx.folder.state == "waiting"
            response = get(url)
            assert response.status_code == 409
            assert response.json()["cells"] == {"folder": "waiting"}

            release_y.set()
            ctx.compute(timeout=30)
            assert ctx.folder.state == "complete"
            assert ctx.folder.checksum == root_y
            response = get(url)
            assert response.status_code == 200
            assert zip_members(response.content) == {
                "folder/payload.txt": payload_y
            }
        finally:
            release_x.set()
            release_y.set()



def test_failed_folder_leaf_recovery_applies_checksum_and_download_reports_503(
        ctx, monkeypatch
    ):
        import seamless_workflow.context as context_module
        from seamless import Buffer, CacheMissError, Checksum
        from seamless.caching.buffer_cache import get_buffer_cache

        payload = b"leaf that recovery cannot recreate"
        child_buffer = Buffer(payload)
        root_buffer = Buffer(
            {"payload.txt": child_buffer.get_checksum().hex()}, "folder"
        )
        root = Checksum(root_buffer.get_checksum())
        child = Checksum(json.loads(bytes(root_buffer.content))["payload.txt"])
        cache = get_buffer_cache()
        with cache.lock:
            cache.strong_cache.pop(child, None)
            cache.weak_cache.pop(child, None)

        def fail(_checksum):
            raise CacheMissError(child.hex())

        monkeypatch.setattr(context_module, "_fingertip_folder_buffer", fail)
        ctx.folder = Cell(celltype="folder")
        url = ctx.shares.add_download(["folder"], path="failed-folder.zip", format="zip")
        ctx.folder.set_checksum(root, input_celltype="folder")
        ctx.compute(timeout=30)

        assert ctx.folder.state == "complete"
        assert ctx.folder.checksum == root
        response = get(url)
        assert response.status_code == 503


class TestDownloadAPI:
    @pytest.mark.parametrize("cells, kwargs, error", [
        (["a/../b"], {}, ValueError),
        (["shares/x"], {}, ValueError),
        (["a", "a/b"], {}, ValueError),
        ({"a": "x", "a/b": "y"}, {}, ValueError),
        (["a"], {"format": "rar"}, ValueError),
        (["a"], {"path": "state-graph"}, ValueError),
        (["a"], {"path": ""}, ValueError),
        ("a", {}, TypeError),
        ([1], {}, TypeError),
    ])
    def test_syntax_errors(self, ctx, cells, kwargs, error):
        """*Errors*: only syntax is refused, before anything is served."""
        with pytest.raises(error):
            ctx.shares.add_download(cells, **kwargs)
        assert ctx.shares.downloads == {}

    def test_remove_download(self, ctx):
        """*Downloads*: removal is immediate; an unknown path is a no-op."""
        url = ctx.shares.add_download(["a"])
        ctx.shares.remove_download()
        assert get(url).status_code == 404
        assert ctx.shares.downloads == {}
        ctx.shares.remove_download()
        ctx.shares.remove_download("never")

    def test_removing_last_download_keeps_namespace_and_state_graph(self, ctx):
        """*The state graph*: removing a download never releases a live namespace."""
        ctx.a = Cell(celltype="int")
        ctx.a.set(1)
        ctx.a.share()
        state_graph = ctx.shares.url + "/state-graph"
        del ctx.a.share
        ctx.shares.add_download(["a"])
        ctx.shares.remove_download()
        assert get(state_graph).status_code == 200

    def test_conflicts_with_shares(self, ctx):
        """*URL space*: a download and a share cannot have the same key."""
        ctx.a = Cell(celltype="int")
        ctx.a.set(1)
        ctx.a.share(path="same")
        with pytest.raises(ValueError):
            ctx.shares.add_download(["a"], path="same")
        ctx.shares.add_download(["a"], path="other")
        ctx.b = Cell(celltype="int")
        ctx.b.set(2)
        with pytest.raises(ValueError):
            ctx.b.share(path="other")
        with pytest.raises(ValueError):
            ctx.shares.add_download(["b"], path="other")

    def test_download_claims_namespace(self, ctx):
        """*Namespaces*: a download claims the namespace like a share."""
        ctx.shares.add_download(["a"])
        with Context() as other:
            with pytest.raises(ValueError, match="in use"):
                other.shares.add_download(["a"])
            other.b = Cell(celltype="int")
            other.b.set(1)
            with pytest.raises(ValueError, match="in use"):
                other.b.share()
        with pytest.raises(ValueError):
            ctx.shares.namespace = "elsewhere"

    def test_not_in_websocket_but_in_openapi(self, ctx):
        """*The websocket*, *openapi.json*: downloads are described, never announced."""
        ctx.shares.namespace = "ws"  # free of records held by earlier tests
        fill(ctx)
        ctx.title.share()
        with updates(ctx.shares.url) as receive:
            receive("Seamless share update server")
            assert set(receive("shares")[1]) == {"title"}
            ctx.shares.add_download(["title"])
            ctx.shares.remove_download()
            ctx.title.set("changed")
            ctx.compute()
            while True:
                message = receive()
                assert message[0] != "shares", message
                if message[0] == "update":
                    break
            assert message[1][0] == "title"
        document = ctx.shares.openapi()
        assert "/ws/download" not in document["paths"]
        ctx.shares.add_download(["title"], format="zip")
        item = ctx.shares.openapi()["paths"]["/ws/download"]
        assert set(item) == {"get"}
        assert item["get"]["x-seamless-download"] == {"format": "zip", "cells": ["title"]}
        assert "application/zip" in item["get"]["responses"]["200"]["content"]


class TestDownloadHold:
    def test_held_download_waits_for_takeover(self, short_hold_server):
        """*The hold on close*: a GET to a held download is answered by the taker."""
        old = Context()
        old.title = Cell(celltype="text")
        old.title.set("old")
        old.compute()
        url = old.shares.add_download(["title"])
        old.close()
        result = {}
        thread = threading.Thread(target=lambda: result.update(response=get(url)))
        thread.start()
        time.sleep(0.2)
        assert thread.is_alive(), "a GET to a held download must wait"
        with Context() as new:
            new.title = Cell(celltype="text")
            new.title.set("new")
            new.compute()
            assert new.shares.add_download(["title"]) == url
            thread.join(10)
            response = result["response"]
            assert response.status_code == 200
            assert tar_members(response.content) == {"title": buffer_of(new, "title")}

    def test_held_download_ends_with_window(self, short_hold_server):
        """*The hold on close*: without a taker, the held download answers 404."""
        old = Context()
        url = old.shares.add_download(["a"])
        old.close()
        assert get(url).status_code == 404


class TestDownloadGraph:
    def test_round_trip(self, ctx):
        """*Graph serialization*: 0.7 writes downloads; shares=True registers them."""
        fill(ctx)
        ctx.shares.add_download({"t": "title"}, path="named", format="zip")
        ctx.shares.add_download(["title"])
        graph = ctx.get_graph()
        assert graph["__seamless_workflow__"] == "0.7"
        assert graph["downloads"] == [
            {"path": "download", "cells": ["title"], "format": "tar.gz"},
            {"path": "named", "cells": {"t": "title"}, "format": "zip"},
        ]
        ctx.shares.remove_download("named")
        ctx.shares.remove_download()
        url = ctx.shares.url + "/named"
        ctx.set_graph(graph)
        assert zip_members(get(url).content) == {"t": buffer_of(ctx, "title")}
        assert set(ctx.shares.downloads) == {"download", "named"}

    def test_set_graph_replaces_and_strips_downloads(self, ctx):
        """*Graph serialization*: the old graph's downloads go; shares=False strips."""
        fill(ctx)
        url = ctx.shares.add_download(["title"])
        graph = ctx.get_graph()
        ctx.set_graph(graph, shares=False)
        assert get(url).status_code == 404
        assert ctx.shares.downloads == {} and "downloads" not in ctx.get_graph()
        ctx.set_graph(graph)
        assert get(url).status_code == 200
        del graph["downloads"]
        ctx.set_graph(graph)
        assert get(url).status_code == 404

    def test_prepare_graph_checks_syntax_only(self, ctx):
        """*Graph serialization*: version, fields and keys are checked; cells are not."""
        graph = ctx.get_graph()
        graph["downloads"] = [{"path": "d", "cells": ["no/such/cell"], "format": "tar"}]
        ctx.set_graph(graph)
        assert get(ctx.shares.url + "/d").json()["cells"] == {"no/such/cell": "missing"}
        for bad in (
            [{"path": "d", "cells": ["a"], "extra": 1}],
            [{"path": "d", "cells": ["a/.."]}],
            [{"path": "d", "cells": ["a"]}, {"path": "d", "cells": ["b"]}],
        ):
            with pytest.raises(PathError, match="Invalid download spec"):
                ctx.set_graph({**graph, "downloads": bad})
        with pytest.raises(PathError, match="0.7"):
            ctx.set_graph({**graph, "__seamless_workflow__": "0.6"})
        ctx.a = Cell(celltype="int")
        ctx.a.set(1)
        ctx.a.share(path="d2")
        shared = ctx.get_graph()
        shared["downloads"] = [{"path": "d2", "cells": ["a"]}]
        with pytest.raises(PathError, match="key of a share"):
            ctx.set_graph(shared)
