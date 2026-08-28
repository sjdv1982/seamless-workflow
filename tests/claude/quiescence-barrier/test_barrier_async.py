"""§14.2 — the async barrier is not a convenience.

    The async form is not a convenience.  A blocking barrier is unusable in
    Jupyter and in any caller that already owns a running loop, which is where
    interactive workflow use actually happens; and the mid-computation tests of
    §15 A0 need to observe state without occupying the thread that would
    otherwise be inspecting it.

So the tests here are not "the same as the sync ones but with ``await``".  Two of
them assert the property that makes the async form necessary: while the barrier
is pending, the caller's event loop keeps running other work, and a concurrent
coroutine can watch the node pass through ``computing``.

Legacy had ``await ctx.computation()`` (``workflow-core/simple-async.py``), and
this suite keeps the name.  Note that on a bound ``Cell``, ``computation`` is
currently *taken*: attribute access is sub-path projection, so
``ctx.a.computation`` resolves to a projection of ``a``'s value and
``ctx.a.computation()`` builds an ``Expression``, which is not awaitable.  The
barrier has to claim the name as a real method, exactly as ``compute``/``run``
are claimed today.
"""

from __future__ import annotations

import asyncio

import pytest

from contract_helpers import (
    SHORT_BODY_SECONDS,
    context_computation,
    node_computation,
    quiescent,
    runtime,
    state,
    states,
)
from seamless_workflow import Context


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


def double(x):
    return 2 * x


def _slow_graph(delay=None):
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 20
    ctx.tf.pins.delay = SHORT_BODY_SECONDS if delay is None else delay
    ctx.tail = double
    ctx.tail.pins.x = ctx.tf
    return ctx


@pytest.mark.a4
@pytest.mark.slow
def test_awaiting_the_context_barrier_settles_the_graph():
    ctx = _slow_graph()

    async def main():
        ctx.tf.pins.y = 22
        assert not quiescent(ctx), states(ctx)
        await context_computation(ctx)

    asyncio.run(main())

    assert quiescent(ctx), states(ctx)
    assert ctx.tail.result.value == 84


@pytest.mark.a4
@pytest.mark.slow
def test_awaiting_the_node_barrier_settles_that_node():
    ctx = _slow_graph()

    async def main():
        ctx.tf.pins.y = 22
        await node_computation(ctx.tail)

    asyncio.run(main())

    assert state(ctx, "tail") == "complete", states(ctx)
    assert ctx.tail.result.value == 84


@pytest.mark.a4
@pytest.mark.slow
def test_the_async_barrier_leaves_the_callers_loop_free():
    """A blocking barrier dressed as a coroutine would fail this."""

    ctx = _slow_graph()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    async def main():
        ctx.tf.pins.y = 22
        background = asyncio.ensure_future(ticker())
        try:
            await context_computation(ctx)
        finally:
            background.cancel()

    asyncio.run(main())

    assert ticks >= 5, f"the caller's loop only advanced {ticks} times during the barrier"
    assert ctx.tail.result.value == 84


@pytest.mark.a4
@pytest.mark.slow
def test_mid_computation_state_is_observable_from_the_same_loop():
    """§15 A0: observe state without occupying the thread that would inspect it.

    This is what legacy's ``ctx.compute(0.5)`` was standing in for.  Legacy could
    only observe mid-computation state by running the barrier *as the pump* in
    the caller's thread; here the graph advances on its own and the observation
    is an ordinary read from a concurrent coroutine.
    """

    ctx = _slow_graph()
    observations = []

    async def observer():
        while True:
            await asyncio.sleep(0.05)
            observations.append(states(ctx))

    async def main():
        ctx.tf.pins.y = 22
        background = asyncio.ensure_future(observer())
        try:
            await context_computation(ctx)
        finally:
            background.cancel()

    asyncio.run(main())

    assert any(entry.get("tf") == "computing" for entry in observations), observations[:5]
    assert any(entry.get("tail") == "waiting" for entry in observations), observations[:5]
    assert all(
        entry.get("tail") != "complete" or entry.get("tf") == "complete"
        for entry in observations
    ), "a downstream node was observed complete while its input was not"
    assert runtime(ctx)["tail"]["state"] == "complete"
