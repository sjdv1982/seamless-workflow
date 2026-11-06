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

    def test_only_get(self, ctx):
        """*The download GET*: every method but GET answers 405."""
        fill(ctx)
        url = ctx.shares.add_download(["title"])
        for method in ("HEAD", "PUT"):
            response = requests.request(method, url, timeout=10)
            assert response.status_code == 405
            assert response.headers["Allow"] == "GET, OPTIONS"

    def test_cut_is_one_read_turn(self, ctx):
        """*The download GET*: the cut is one read turn of the Context."""
        fill(ctx)
        url = ctx.shares.add_download(["title", "sub/table", "raw"])
        def cuts():
            return [entry[0] for entry in list(ctx._controller.trace) if entry[2] == "_download_cut"]

        before = set(cuts())
        assert get(url).status_code == 200
        assert len(set(cuts()) - before) == 1

    def test_graph_edits_never_touch_a_download(self, ctx):
        """*Downloads*: paths are checked only by each GET; no graph edit is refused."""
        url = ctx.shares.add_download(["later"])
        assert get(url).json()["cells"] == {"later": "missing"}
        ctx.later = Cell(celltype="int")
        ctx.later.set(5)
        ctx.compute()
        assert tar_members(get(url).content) == {"later": buffer_of(ctx, "later")}
        del ctx.later
        assert get(url).json()["cells"] == {"later": "missing"}
        assert ctx.shares.downloads["download"].cells == (("later", "later"),)


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
