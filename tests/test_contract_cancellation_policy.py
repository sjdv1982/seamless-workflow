"""Contract: the reactive Context uses softcancel exclusively
(contracts/cancellation.md, "Policy: which operation, where").

The Context only ever *loses interest*: node supersession, node deletion,
``prune()`` and ``close()`` must deregister the Context's participation and
never hard-cancel the checksum. Observable consequence: a standalone peer
handle sharing the Context node's tf_checksum keeps its run and gets the
result, from the one execution, and ``TransformationCache.cancel_by_checksum``
(the hard verb) is never called.
"""

import os
import threading
import time

import pytest

from seamless_transformer import delayed
from seamless_transformer import transformation_cache as tcm


def marked_slow(x, d):
    import os
    import time
    import uuid

    open(os.path.join(d, uuid.uuid4().hex), "w").close()
    time.sleep(2.0)
    return x * 2


@pytest.fixture
def hard_cancels(monkeypatch):
    calls = []
    original = tcm.TransformationCache.cancel_by_checksum

    def spy(self, tf_checksum, **kwargs):
        calls.append(tf_checksum)
        return original(self, tf_checksum, **kwargs)

    monkeypatch.setattr(tcm.TransformationCache, "cancel_by_checksum", spy)
    return calls


def _wait_members(n, timeout=10.0):
    cache = tcm.get_transformation_cache()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sizes = [len(a.awaiters) for a in list(cache._active_submissions.values())]
        if sizes and max(sizes) >= n:
            return
        time.sleep(0.01)
    raise AssertionError(f"no membership set reached {n} members")


def _start_peer(x, d, out):
    def run():
        try:
            out.append(delayed(marked_slow)(x, d).run())
        except BaseException as exc:  # noqa: BLE001
            out.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _node_with_peer(make_context, tmp_path):
    d = str(tmp_path)
    x = time.time()
    ctx = make_context()
    ctx.tf = marked_slow
    ctx.tf.pins.d = d
    ctx.tf.pins.x = x
    _wait_members(1)
    out = []
    thread = _start_peer(x, d, out)
    _wait_members(2)
    return ctx, d, x, out, thread


def _executions_of(d):
    return len(os.listdir(d))


def test_supersession_softcancels_and_peer_survives(make_context, tmp_path, hard_cancels):
    ctx, d, x, out, thread = _node_with_peer(make_context, tmp_path)
    ctx.tf.pins.x = x + 1  # supersede the running node
    thread.join(20)
    assert out == [x * 2]
    assert hard_cancels == []
    ctx.compute(timeout=20)
    assert ctx.tf.result.value == (x + 1) * 2
    assert _executions_of(d) == 2  # x once (shared), x + 1 once


def test_node_deletion_softcancels_and_peer_survives(make_context, tmp_path, hard_cancels):
    ctx, d, x, out, thread = _node_with_peer(make_context, tmp_path)
    del ctx.tf
    thread.join(20)
    assert out == [x * 2]
    assert hard_cancels == []
    assert _executions_of(d) == 1


def test_prune_softcancels_and_peer_survives(make_context, tmp_path, hard_cancels):
    ctx, d, x, out, thread = _node_with_peer(make_context, tmp_path)
    ctx.tf.pins.x = x + 1
    ctx.prune()  # drop the superseded run that the peer shares
    thread.join(20)
    assert out == [x * 2]
    assert hard_cancels == []


@pytest.mark.xfail(
    strict=False,
    reason=(
        "cancellation.md 'The pattern' + 'Policy': Context.close() must only "
        "lose interest. The shared run's background task lives on the Context "
        "controller's loop (the first caller's loop); stopping that loop on "
        "close cancels it, so a peer handle gets ExecutionCanceledError."
    ),
)
def test_close_softcancels_and_peer_survives(make_context, tmp_path, hard_cancels):
    ctx, d, x, out, thread = _node_with_peer(make_context, tmp_path)
    ctx.close()
    thread.join(20)
    assert hard_cancels == []
    assert out == [x * 2]
    assert _executions_of(d) == 1
