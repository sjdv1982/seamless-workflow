"""The legacy ``compute(timeout)`` question, decided.

Legacy ``ctx.compute(0.5)`` conflated three things:

1. **a pump** — in legacy the event loop ran in the caller's thread, so the
   barrier was what advanced the computation and nothing progressed without it;
2. **an observation window** — it was the only way to look at a graph
   mid-computation;
3. **a hang guard** — an upper bound on how long a wedged graph could block a
   script.

A dedicated controller thread (§4) removes (1) outright.  (2) becomes a plain
``time.sleep`` or ``await asyncio.sleep``, because the graph advances while the
caller is doing nothing.  (3) belongs to the caller — §14.2: "Neither form takes
a timeout: a stuck E/T is a hang to be caught by the caller's own timeout
mechanism, not something a barrier should paper over by returning as if
quiescent."

So this suite ports the *requirement* and not the *signature*, and the tests
below are written to survive either implementation choice.  The assertion is
never "``compute`` rejects an argument"; it is **a barrier never returns a
non-quiescent graph**.  A ``timeout=`` keyword that raises on expiry is
compatible with that; a timeout that returns early, silently, as if settled, is
not — that is the pump, and it is what makes "did my computation finish?"
unanswerable from the return value.

(The design is not internally consistent on the word *timeout*: §14.2 says
barriers take none, while [MOD-17] says "barriers need timeouts".  They are
about different objects — a graph-quiescence barrier, versus the external
finite-cut barrier of §21 and its delivery deadlines, which bound external I/O
and cannot be left to the caller.  These tests only constrain the first.)
"""

from __future__ import annotations

import time

import pytest

from contract_helpers import (
    PENDING,
    PROMPT_SECONDS,
    SHORT_BODY_SECONDS,
    context_compute,
    quiescent,
    settle,
    state,
    states,
    timed,
)
from seamless_workflow import Context


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


@pytest.mark.a4
@pytest.mark.slow
def test_a_barrier_never_returns_a_non_quiescent_graph():
    """Whatever a timeout argument means, it must not mean "give up quietly"."""

    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.y = 2

    try:
        context_compute(ctx, 0.5)
    except TypeError:
        pass  # no timeout argument: the legacy pump was not ported at all
    else:
        assert quiescent(ctx), (
            "a barrier returned while the graph was still pending: the legacy "
            "timeout was ported as a pump"
        )


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

    assert state(ctx, "tf") in PENDING, states(ctx)
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

    assert settle(ctx, timeout=SHORT_BODY_SECONDS * 10), states(ctx)
    assert ctx.tf.result.value == 42
