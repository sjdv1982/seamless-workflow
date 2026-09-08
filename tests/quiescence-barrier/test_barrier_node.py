"""§14.2/§10 — the node-local quiescence barrier.

    ctx.a.compute()               # returns when a's cone is quiescent, then reads a
    await ctx.a.computation()

``ctx.a.compute()`` is the correlated ``NodeQuiescenceBarrier(a)`` +
``ReadChecksum(a)`` pair of §10: it waits, then reads, and it keeps returning a
checksum where the standalone form does — quiescence is an added guarantee, not
a replacement of the return value.  For a Transformer node it additionally
implies that the node's submission has delivered, which is not an extra
condition but the same one: the node stays ``computing`` until the class-5
notification arrives.

The name is currently pinned as a no-op by ``test_canonical_handles.py:143``
(``assert ctx.data.compute() == ctx.data.checksum``).  That assertion stays true
here — it is the ``now`` test below — because for a node that is already
``complete`` the barrier is satisfied on arrival.  What is missing is everything
the barrier does when the node is *not* complete.
"""

from __future__ import annotations

import pytest

from contract_helpers import (
    SHORT_BODY_SECONDS,
    quiescent,
    states,
    timed,
)
from seamless import Checksum
from seamless_workflow import Context
from seamless_workflow.errors import NodeError


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


def double(x):
    return 2 * x


@pytest.mark.now
def test_a_node_barrier_on_a_settled_cell_returns_its_checksum():
    """The pinned contract of ``test_canonical_handles.py``, restated as a barrier case."""

    ctx = Context()
    ctx.data = {"n": 4}

    assert ctx.data.compute() == ctx.data.checksum
    assert ctx.data.run() == {"n": 4}


@pytest.mark.now
def test_a_node_barrier_raises_on_unwired_and_blocked_nodes():
    """Current behaviour, pinned so that no phase changes it by accident.

    Note this differs from the Context-wide barrier, which §24.4 settles as
    *returns*.  Whether the node-local form should raise or return is recorded
    as an open question in this suite's README; the test exists so the answer is
    chosen rather than drifted into.
    """

    ctx = Context()
    ctx.tf = double  # no pin: unwired
    ctx.out = ctx.tf

    with pytest.raises(NodeError):
        ctx.tf.compute()
    with pytest.raises(NodeError):
        ctx.out.compute()


@pytest.mark.a4
@pytest.mark.slow
def test_a_node_barrier_waits_for_its_own_cone_and_then_reads():
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 20
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tail = double
    ctx.tail.pins.x = ctx.tf

    ctx.tf.pins.y = 22
    assert ctx.tail.state == "waiting", states(ctx)

    checksum = ctx.tail.compute()

    assert isinstance(checksum, Checksum)
    assert ctx.tail.state == "complete", states(ctx)
    assert checksum == ctx.tail.result.checksum
    assert ctx.tail.run() == 84


@pytest.mark.a4
@pytest.mark.slow
def test_a_node_barrier_waits_only_for_its_own_cone():
    """Node-local, not Context-wide: an unrelated slow branch must not delay it.

    This is the test that distinguishes the two barriers.  Both would pass a
    "the value is right" assertion; only this one fails if ``ctx.a.compute()``
    is implemented as ``ctx.compute()`` followed by a read.
    """

    ctx = Context()
    ctx.slow = slow_add
    ctx.slow.pins.x = 1
    ctx.slow.pins.y = 2
    ctx.slow.pins.delay = SHORT_BODY_SECONDS * 2

    ctx.fast = slow_add
    ctx.fast.pins.x = 3
    ctx.fast.pins.y = 4
    ctx.fast.pins.delay = 0

    with timed() as elapsed:
        assert ctx.fast.run() == 7

    assert elapsed.seconds < SHORT_BODY_SECONDS, elapsed.seconds
    assert ctx.fast.state == "complete", states(ctx)
    assert not quiescent(ctx), "the unrelated slow branch should still be pending"


@pytest.mark.a4
@pytest.mark.slow
def test_a_cell_node_barrier_waits_for_the_transformer_that_feeds_it():
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.out = ctx.tf

    ctx.tf.pins.y = 2
    assert ctx.out.state == "waiting", states(ctx)

    checksum = ctx.out.compute()

    assert checksum == ctx.out.checksum
    assert ctx.out.value == 3
    assert ctx.out.state == "complete"


def fails(x):
    raise RuntimeError('upstream failure')


@pytest.mark.a4
@pytest.mark.slow
def test_failed_target_barrier_still_waits_for_pending_upstream_sibling():
    """A terminal target does not imply its whole upstream cone is quiescent."""
    ctx = Context()
    ctx.failed = fails
    ctx.failed.pins.x = 1
    with pytest.raises(RuntimeError):
        ctx.failed.compute(timeout=10)
    ctx.slow = slow_add
    ctx.slow.pins.x = 10
    ctx.slow.pins.y = 20
    ctx.slow.pins.delay = SHORT_BODY_SECONDS
    ctx.join = slow_add
    ctx.join.pins.x = ctx.failed
    ctx.join.pins.y = ctx.slow
    ctx.join.pins.delay = 0
    assert ctx.join.state == 'blocked'
    assert ctx.slow.state == 'computing'
    with pytest.raises(TimeoutError):
        ctx.join.compute(timeout=.01)
    # Once the remaining upstream work settles, the node error is delivered.
    ctx.slow.compute(timeout=10)
    with pytest.raises(NodeError):
        ctx.join.compute(timeout=10)
