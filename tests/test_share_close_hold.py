"""Focused contract checks for holding share URLs when a Context closes."""

import asyncio
import queue
import threading
import time
from contextlib import contextmanager

import aiohttp
import pytest
import requests

from seamless import Cell
from seamless_workflow import Context, shareserver


@pytest.fixture
def server():
    shareserver.configure(host="127.0.0.1", port=0)
    yield shareserver
    shareserver.close()


def get(url, **kwargs):
    return requests.get(url, timeout=10, **kwargs)


def shared_int(ctx, value, name="a", *, readonly=True):
    setattr(ctx, name, Cell(celltype="int"))
    getattr(ctx, name).set(value)
    getattr(ctx, name).share(readonly=readonly)
    return getattr(ctx, name).share.url


@contextmanager
def websocket(url):
    messages = queue.Queue()
    ready = threading.Event()

    async def read():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url.replace("http://", "ws://")) as ws:
                    ready.set()
                    while True:
                        item = await ws.receive(timeout=10)
                        if item.type == aiohttp.WSMsgType.TEXT:
                            messages.put(item.json())
                        elif item.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                            messages.put(("closed", ws.close_code))
                            return
                        elif item.type == aiohttp.WSMsgType.ERROR:
                            raise ws.exception()
        except BaseException as exc:
            messages.put(exc)
            ready.set()

    worker = threading.Thread(target=lambda: asyncio.run(read()), daemon=True)
    worker.start()
    assert ready.wait(10), "websocket did not connect"
    try:
        yield messages
    finally:
        worker.join(10)
        assert not worker.is_alive(), "websocket reader did not stop"


def test_close_keeps_http_record_openapi_and_state_graph(server):
    ctx = Context()
    try:
        ctx.shares.namespace = "held-http"
        url = shared_int(ctx, 7)
        before = get(url)
        checksum = get(url, params={"mode": "checksum"})
        state_url = ctx.shares.url + "/state-graph"
        state_before = get(state_url)
        assert before.status_code == checksum.status_code == state_before.status_code == 200

        ctx.close()

        held = get(url)
        assert held.status_code == 200 and held.json() == 7
        assert held.headers["X-Seamless-Marker"] == before.headers["X-Seamless-Marker"]
        assert get(url, params={"mode": "checksum"}).text == checksum.text
        head = requests.head(url, timeout=10)
        assert head.status_code == 200
        assert get(state_url).status_code == 200
        assert f"/held-http/a" in get(server.url + "/openapi.json").json()["paths"]
    finally:
        ctx.close()


def test_takeover_preserves_equal_value_marker_and_uses_new_context_value(server):
    old = Context()
    new = None
    try:
        old.shares.namespace = "held-takeover"
        url = shared_int(old, 7, readonly=False)
        before = get(url)
        marker = before.headers["X-Seamless-Marker"]
        old.close()

        new = Context()
        new.shares.namespace = "held-takeover"
        new_url = shared_int(new, 7, readonly=False)
        assert new_url == url
        assert get(url).headers["X-Seamless-Marker"] == marker
        assert new.a.value == 7

        new.a.set(8)
        new.mounts.sync(timeout=10)
        changed = get(url)
        assert changed.json() == 8
        assert int(changed.headers["X-Seamless-Marker"]) == int(marker) + 1
        assert new.a.value == 8
    finally:
        old.close()
        if new is not None:
            new.close()


def test_held_share_does_not_feed_a_replacement_context(server):
    old = Context()
    new = None
    try:
        old.shares.namespace = "held-no-feed"
        url = shared_int(old, 7, readonly=False)
        old.close()

        new = Context()
        new.shares.namespace = "held-no-feed"
        new.a = Cell(celltype="int")
        new.a.set(11)
        new.a.share(readonly=False)
        assert new.a.share.url == url
        assert new.a.value == 11
        assert get(url).json() == 11
    finally:
        old.close()
        if new is not None:
            new.close()


def test_non_close_detach_removes_url_immediately(server):
    with Context() as ctx:
        ctx.shares.namespace = "detach-immediate"
        url = shared_int(ctx, 3)
        assert get(url).status_code == 200
        del ctx.a.share
        assert get(url).status_code == 404


def test_namespace_change_after_partial_takeover_releases_old_namespace(server):
    old = Context()
    replacement = None
    later = None
    try:
        old.shares.namespace = "partial-hold"
        retained_url = shared_int(old, 1, name="a")
        untouched_url = shared_int(old, 2, name="b")
        state_url = old.shares.url + "/state-graph"
        assert get(state_url).status_code == 200
        with websocket(old.shares.url) as messages:
            assert messages.get(timeout=10) == ["Seamless share update server", "1.0"]
            assert messages.get(timeout=10)[0] == "shares"
            old.close()

            replacement = Context()
            replacement.shares.namespace = "partial-hold"
            assert shared_int(replacement, 1, name="a") == retained_url
            del replacement.a.share
            replacement.shares.namespace = "moved"

            assert get(untouched_url).status_code == 404
            assert get(state_url).status_code == 404
            while True:
                message = messages.get(timeout=10)
                if isinstance(message, BaseException):
                    raise message
                if message[0] == "closed":
                    assert message[1] == 1001
                    break

        later = Context()
        later.shares.namespace = "partial-hold"
        assert shared_int(later, 3, name="c").endswith("/partial-hold/c")
        assert get(later.c.share.url).json() == 3
    finally:
        old.close()
        if replacement is not None:
            replacement.close()
        if later is not None:
            later.close()


def test_unclaimed_hold_expires_and_releases_namespace(server):
    shareserver.configure(host="127.0.0.1", port=0, hold_timeout=0.3)
    old = Context()
    later = None
    try:
        old.shares.namespace = "expired-hold"
        url = shared_int(old, 7)
        state_url = old.shares.url + "/state-graph"
        assert get(state_url).status_code == 200
        with websocket(old.shares.url) as messages:
            assert messages.get(timeout=10) == ["Seamless share update server", "1.0"]
            assert messages.get(timeout=10)[0] == "shares"
            old.close()
            assert get(url).json() == 7

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if get(url).status_code == get(state_url).status_code == 404:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("unclaimed held routes remained after the hold window")

            while True:
                message = messages.get(timeout=10)
                if isinstance(message, BaseException):
                    raise message
                if message[0] == "closed":
                    assert message[1] == 1001
                    break

        later = Context()
        later.shares.namespace = "expired-hold"
        assert shared_int(later, 9).endswith("/expired-hold/a")
        assert get(later.a.share.url).json() == 9
    finally:
        old.close()
        if later is not None:
            later.close()
