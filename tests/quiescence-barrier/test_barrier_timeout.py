"""§27 — every barrier form takes an optional ``timeout``.

    ctx.compute(timeout=None)
    await ctx.computation(timeout=None)
    ctx.a.compute(timeout=None)
    await ctx.a.computation(timeout=None)

Four rules, one test each, plus the rule that ties them together:

1. no ``timeout`` means wait — the §14.2 behaviour, unchanged;
2. **expiry raises ``TimeoutError``; it never returns.**  This is the property
   that keeps the §26.2 invariant intact — *a barrier never returns a
   non-quiescent graph* — because on the timeout path it returns nothing at all;
3. the deadline bounds the caller's wait and nothing else: the computation is
   untouched, keeps running across the expiry, and a later barrier returns its
   result;
4. a deadline is not a pump: expiring advances nothing, and neither does
   waiting.  ``compute(timeout=X)`` and ``sleep(X)`` differ only in that the
   first stops early when the graph settles and raises when it does not.

Rule 2 is the one to protect.  A ``timeout`` that returned early as if settled
would be the legacy pump wearing a modern name, and would make "did my
computation finish?" unanswerable from the return value.
"""

from __future__ import annotations

import asyncio

import pytest

from contract_helpers import (
    PENDING,
    PROMPT_SECONDS,
    SHORT_BODY_SECONDS,
    quiescent,
    settle,
    states,
    timed,
)
from seamless import Checksum
from seamless_workflow import Context


#: Short enough that it certainly expires mid-body, long enough not to be noise.
DEADLINE: float = 0.05


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


def _pending_graph():
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 20
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.y = 22
    return ctx


@pytest.mark.a3
def test_a_barrier_without_a_timeout_still_waits():
    """§27.1.1: ``timeout=None`` is the default and changes nothing.

    Stated on a graph that settles without executing anything, so it holds
    through the limbo as well as after it.
    """

    ctx = Context()
    ctx.a = 1
    ctx.b = ctx.a

    ctx.compute()

    assert quiescent(ctx), states(ctx)


@pytest.mark.a4
@pytest.mark.slow
def test_an_expired_sync_barrier_raises_rather_than_returning():
    """§27.1.2 — the rule the whole appendix exists to protect."""

    ctx = _pending_graph()

    with timed() as elapsed:
        with pytest.raises(TimeoutError):
            ctx.compute(timeout=DEADLINE)

    assert elapsed.seconds < SHORT_BODY_SECONDS, (
        f"the barrier ignored its deadline and waited {elapsed.seconds:.2f}s"
    )
    assert not quiescent(ctx), states(ctx)


@pytest.mark.a4
@pytest.mark.slow
def test_an_expired_sync_barrier_leaves_the_computation_running():
    """§27.1.3: a deadline is an observation, never a control operation."""

    ctx = _pending_graph()

    with pytest.raises(TimeoutError):
        ctx.compute(timeout=DEADLINE)

    assert ctx.tf.state in PENDING, states(ctx)
    assert ctx.tf.result.checksum is None

    ctx.compute()

    assert quiescent(ctx), states(ctx)
    assert ctx.tf.result.value == 42


@pytest.mark.a4
@pytest.mark.slow
def test_an_expired_async_barrier_raises_and_leaves_the_computation_running():
    """The async twin.  ``asyncio.wait_for`` would abandon; this withdraws (§27.1.5)."""

    ctx = _pending_graph()

    async def main():
        with pytest.raises(TimeoutError):
            await ctx.computation(timeout=DEADLINE)
        assert ctx.tf.state in PENDING, states(ctx)

        await ctx.computation()

    asyncio.run(main())

    assert quiescent(ctx), states(ctx)
    assert ctx.tf.result.value == 42


@pytest.mark.a4
@pytest.mark.slow
def test_the_node_local_barrier_takes_a_timeout_too():
    """§27: all four forms, not only the Context-wide pair.

    The node-local form returns a checksum where it returns at all (§10), so the
    two outcomes are easy to confuse: a barrier that returned ``None`` on expiry
    would read like a node with no result rather than like a deadline.  It
    raises instead.
    """

    ctx = _pending_graph()

    with pytest.raises(TimeoutError):
        ctx.tf.compute(timeout=DEADLINE)

    checksum = ctx.tf.compute()

    assert isinstance(checksum, Checksum)
    assert ctx.tf.result.value == 42


@pytest.mark.a4
@pytest.mark.slow
def test_the_async_node_local_barrier_takes_a_timeout_too():
    ctx = _pending_graph()

    async def main():
        with pytest.raises(TimeoutError):
            await ctx.tf.computation(timeout=DEADLINE)
        return await ctx.tf.computation()

    checksum = asyncio.run(main())

    assert isinstance(checksum, Checksum)
    assert ctx.tf.result.value == 42


@pytest.mark.a4
@pytest.mark.slow
def test_repeated_expiry_does_not_accumulate_predicates():
    """§27.1.5 — the reason the parameter cannot live outside the API.

    [MOD-17] has a barrier install a completion predicate that the controller
    re-evaluates at the end of every turn.  A caller polling with a short
    deadline must not leave one behind per call, or the controller's per-turn
    work grows without bound for the lifetime of the Context.

    Stated as something observable from outside: after many expiries the graph
    still settles, and it still settles *promptly* once the body is done.  A
    suite cannot count the controller's predicates from here; it can notice when
    they start costing something.
    """

    ctx = _pending_graph()

    expiries = 0
    for _ in range(20):
        try:
            ctx.compute(timeout=DEADLINE)
        except TimeoutError:
            expiries += 1
        else:
            break

    assert expiries >= 1, "the body finished before a single deadline expired"

    settle(ctx, timeout=SHORT_BODY_SECONDS * 10)

    with timed() as elapsed:
        ctx.compute()

    assert elapsed.seconds < PROMPT_SECONDS, (
        f"a settled graph took {elapsed.seconds:.2f}s to clear its barrier after "
        f"{expiries} expiries; predicates are accumulating"
    )
    assert ctx.tf.result.value == 42


@pytest.mark.a4
@pytest.mark.slow
def test_a_deadline_never_returns_a_non_quiescent_graph():
    """§26.2's invariant, restated against the parameter that could break it.

    The general form of rule 2: whatever a barrier returns, the graph was
    quiescent when it did.  Every path through a timed barrier either raises or
    satisfies this.
    """

    ctx = _pending_graph()

    for deadline in (DEADLINE, SHORT_BODY_SECONDS * 4):
        try:
            ctx.compute(timeout=deadline)
        except TimeoutError:
            continue
        assert quiescent(ctx), (
            f"compute(timeout={deadline}) returned on a non-quiescent graph: the "
            f"legacy pump was ported under a new name — {states(ctx)}"
        )
