from __future__ import annotations

import asyncio
from uuid import uuid4

from seamless import Checksum


def slow_identity(value, delay, nonce):
    import time

    time.sleep(delay)
    return value


def _node(context, name):
    return context._graph.nodes[(name,)]


def _two_independent_runs(context):
    context.short = slow_identity
    context.long = slow_identity
    context.short.pins.delay = 0.15
    context.long.pins.delay = 0.75
    context.short.pins.nonce = uuid4().hex
    context.long.pins.nonce = uuid4().hex
    context.short.pins.value = "short"
    context.long.pins.value = "long"


def test_node_compute_waits_only_for_the_nodes_upstream_cone(make_context):
    context = make_context()
    _two_independent_runs(context)

    checksum = context.short.compute()

    assert isinstance(checksum, Checksum)
    assert _node(context, "short").state == "complete"
    assert context.short.result.value == "short"
    assert _node(context, "long").state == "computing"
    assert _node(context, "long").current_checksum is None
    context.compute()


def test_async_node_computation_waits_and_returns_the_node_checksum(make_context):
    context = make_context()
    context.slow = slow_identity
    context.slow.pins.delay = 0.25
    context.slow.pins.nonce = uuid4().hex
    context.slow.pins.value = {"answer": 42}

    async def scenario():
        barrier = asyncio.create_task(context.slow.computation())
        await asyncio.sleep(0)
        assert not barrier.done()
        checksum = await asyncio.wait_for(barrier, timeout=2.0)
        assert isinstance(checksum, Checksum)
        return checksum

    checksum = asyncio.run(scenario())
    assert checksum == context.slow.result.checksum
    assert context.slow.result.value == {"answer": 42}


def test_cell_compute_is_correlated_barrier_plus_checksum_read(make_context):
    context = make_context()
    context.slow = slow_identity
    context.slow.pins.delay = 0.2
    context.slow.pins.nonce = uuid4().hex
    context.slow.pins.value = 17
    context.answer = context.slow

    assert _node(context, "slow").state == "computing"
    assert _node(context, "answer").state == "waiting"
    assert _node(context, "answer").current_checksum is None

    checksum = context.answer.compute()

    assert isinstance(checksum, Checksum)
    assert checksum == context.answer.checksum
    assert context.answer.value == 17
    assert _node(context, "answer").state == "complete"
