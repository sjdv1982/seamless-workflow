"""The graph advances by itself; waiting on it advances nothing.

Legacy ``ctx.compute(0.5)`` conflated three things:

1. **a pump** — in legacy the event loop ran in the caller's thread, so the
   barrier was what advanced the computation and nothing progressed without it;
2. **an observation window** — it was the only way to look at a graph
   mid-computation;
3. **a hang guard** — an upper bound on how long a wedged graph could block a
   script.

A dedicated controller thread (§4) removes (1) outright.  (2) becomes a plain
``time.sleep`` or ``await asyncio.sleep``, because the graph advances while the
caller is doing nothing.  (3) is kept, and kept *in the API*, as the ``timeout``
of §27 — see ``test_barrier_timeout.py`` for that half.

This file is the other half: **nothing in it calls a barrier at all.**  If any
test here needed one, the suite would have quietly reintroduced the pump.  It is
therefore one of the three places that use ``contract_helpers.settle`` — the
pure poller — rather than ``compute_or_settle``: here the barrier is the
subject, and a test that reached its own precondition through ``compute`` could
not falsify anything about ``compute``.  These tests are what make ``settle()``
an honest instrument: it polls, it never calls ``compute``, and the graph
reaches quiescence anyway.
"""

from __future__ import annotations

import time

import pytest

from contract_helpers import (
    PENDING,
    PROMPT_SECONDS,
    SHORT_BODY_SECONDS,
    settle,
    states,
    timed,
)
from seamless_workflow import Context


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


@pytest.mark.a1
@pytest.mark.slow
def test_intermediate_state_is_observable_with_a_plain_sleep():
    """What legacy's ``compute(0.5)`` was standing in for, without the pump."""

    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    with timed() as elapsed:
        ctx.tf.pins.y = 2
    assert elapsed.seconds < PROMPT_SECONDS, elapsed.seconds

    time.sleep(SHORT_BODY_SECONDS / 4)

    assert ctx.tf.state in PENDING, states(ctx)
    assert ctx.tf.result.checksum is None


@pytest.mark.a4
@pytest.mark.slow
def test_the_graph_advances_with_no_barrier_call_at_all():
    """The positive half: nothing in this test pumps anything."""

    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    with timed() as elapsed:
        ctx.tf.pins.y = 2
    assert elapsed.seconds < PROMPT_SECONDS, elapsed.seconds

    settle(ctx, timeout=SHORT_BODY_SECONDS * 10)
    assert ctx.tf.result.value == 42
