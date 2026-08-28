from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest


def slow_identity(value, delay, nonce):
    import time

    time.sleep(delay)
    return value


def explode(value):
    raise RuntimeError(f"barrier failure: {value}")


def increment(value):
    return value + 1


def _node(context, name):
    return context._graph.nodes[(name,)]


def test_context_compute_waits_for_every_nonterminal_node(make_context):
    """Port the finite legacy ``ctx.compute()`` barrier, not its event pump."""

    context = make_context()
    context.left = slow_identity
    context.right = slow_identity
    context.left.pins.delay = 0.15
    context.right.pins.delay = 0.35
    context.left.pins.nonce = uuid4().hex
    context.right.pins.nonce = uuid4().hex
    context.left.pins.value = "left"
    context.right.pins.value = "right"

    assert _node(context, "right").state == "computing"
    context.compute()

    assert _node(context, "left").state == "complete"
    assert _node(context, "right").state == "complete"
    assert context.left.result.value == "left"
    assert context.right.result.value == "right"


def test_async_context_barrier_yields_the_callers_event_loop(make_context):
    """Port legacy ``ctx.computation()`` without caller-loop ownership."""

    context = make_context()
    context.slow = slow_identity
    context.slow.pins.delay = 0.25
    context.slow.pins.nonce = uuid4().hex
    context.slow.pins.value = 42

    async def scenario():
        barrier = asyncio.create_task(context.computation())
        await asyncio.sleep(0)
        assert not barrier.done()
        await asyncio.wait_for(barrier, timeout=2.0)

    asyncio.run(scenario())
    assert _node(context, "slow").state == "complete"
    assert context.slow.result.value == 42


def test_context_barrier_returns_when_failures_and_blocks_are_terminal(make_context):
    context = make_context()
    context.bad = explode
    context.after = increment
    context.after.pins.value = context.bad
    context.bad.pins.value = 9

    context.compute()

    assert _node(context, "bad").state == "failed"
    assert _node(context, "after").state == "blocked"
    assert _node(context, "after").block_reason == "blocked-by-error"


def test_sync_timeout_bounds_only_the_wait_and_does_not_stop_progress(make_context):
    """Retain the useful deadline while removing legacy timeout-pumping."""

    context = make_context()
    context.slow = slow_identity
    context.slow.pins.delay = 0.35
    context.slow.pins.nonce = uuid4().hex
    context.slow.pins.value = "eventual result"

    with pytest.raises(TimeoutError):
        context.compute(timeout=0.05)

    assert _node(context, "slow").state == "computing"
    assert _node(context, "slow").current_checksum is None
    context.compute()
    assert context.slow.result.value == "eventual result"


def test_async_timeout_bounds_only_the_wait_and_does_not_stop_progress(make_context):
    context = make_context()
    context.slow = slow_identity
    context.slow.pins.delay = 0.35
    context.slow.pins.nonce = uuid4().hex
    context.slow.pins.value = "eventual async result"

    async def scenario():
        with pytest.raises(TimeoutError):
            await context.computation(timeout=0.05)
        assert _node(context, "slow").state == "computing"
        await context.computation()

    asyncio.run(scenario())
    assert context.slow.result.value == "eventual async result"
