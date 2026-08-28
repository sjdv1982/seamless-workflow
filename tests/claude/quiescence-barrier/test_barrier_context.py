"""§14.2 — the Context-wide quiescence barrier.

    ctx.compute()                 # returns when no node is waiting or computing
    await ctx.computation()       # the same barrier, without blocking the caller

Because results stop being synchronous (§14.1), the public API needs explicit
barriers.  ``Context.compute`` does not exist today at all: ``Context.__getattr__``
answers any unknown name with a ``MissingView``, so ``ctx.compute`` is not an
``AttributeError`` — it is a namespace handle that is not callable.  Every test
here therefore goes through ``contract_helpers.context_compute``, which looks the
method up on the class and fails with a sentence.

Two settled decisions are asserted rather than described:

* **§24.4** — the barrier *returns* when part of the graph is ``blocked`` or
  ``failed``.  ``blocked`` is terminal until a class-2 or class-3 message
  arrives, so waiting for it would hang by construction; the caller inspects
  ``.exception``.
* **[MOD-17]** — a barrier installs a completion predicate and ends its turn.
  It never stalls the controller's processed frontier, because reaching
  quiescence requires the controller to keep processing exactly the class-5
  notifications a stall would defer.  A barrier whose predicate already holds
  resolves in its own turn.
"""

from __future__ import annotations

import threading

import pytest

from contract_helpers import (
    PROMPT_SECONDS,
    SHORT_BODY_SECONDS,
    context_compute,
    quiescent,
    state,
    states,
    timed,
)
from seamless_workflow import Context


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


def double(x):
    return 2 * x


def boom(x):
    raise RuntimeError("boom")


@pytest.mark.a3
def test_the_context_wide_barrier_exists_and_is_callable():
    ctx = Context()
    ctx.a = 1

    context_compute(ctx)

    assert quiescent(ctx)


@pytest.mark.a3
def test_a_barrier_whose_predicate_already_holds_returns_at_once():
    """[MOD-17]: no round trip through anything slow when there is nothing to wait for."""

    ctx = Context()
    ctx.a = 1
    ctx.b = ctx.a

    with timed() as elapsed:
        context_compute(ctx)

    assert elapsed.seconds < PROMPT_SECONDS, elapsed.seconds
    assert quiescent(ctx)


@pytest.mark.a3
def test_the_barrier_returns_on_an_unwired_graph():
    """§24.4, the half that needs no execution: ``unwired`` is terminal, not pending."""

    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 1  # y and delay never set

    with timed() as elapsed:
        context_compute(ctx)

    assert elapsed.seconds < PROMPT_SECONDS, elapsed.seconds
    assert state(ctx, "tf") == "unwired"


@pytest.mark.a4
@pytest.mark.slow
def test_the_barrier_returns_only_when_no_node_is_pending():
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 20
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tail = double
    ctx.tail.pins.x = ctx.tf
    ctx.out = ctx.tail

    ctx.tf.pins.y = 22
    assert not quiescent(ctx), states(ctx)

    context_compute(ctx)

    assert quiescent(ctx), states(ctx)
    assert ctx.out.value == 84


@pytest.mark.a4
@pytest.mark.slow
def test_the_barrier_returns_on_a_failed_graph():
    """§24.4: quiescence is the contract; the caller inspects ``.exception``."""

    ctx = Context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail

    context_compute(ctx)

    assert quiescent(ctx), states(ctx)
    assert state(ctx, "fail") == "failed"
    assert state(ctx, "tail") == "blocked"
    assert isinstance(ctx.fail.exception, BaseException)


@pytest.mark.a4
@pytest.mark.slow
def test_a_pending_barrier_does_not_stall_the_processed_frontier():
    """[MOD-17], stated as something a second thread can observe.

    While one thread waits on the barrier, an ordinary public write from
    another thread must still be accepted and applied.  A barrier that held the
    frontier would make this write wait for quiescence — and, in the general
    case, deadlock.
    """

    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.y = 2

    barrier_done = threading.Event()
    failures: list[BaseException] = []

    def wait_for_quiescence():
        try:
            context_compute(ctx)
        except BaseException as exc:  # re-raised in the main thread
            failures.append(exc)
        finally:
            barrier_done.set()

    waiter = threading.Thread(target=wait_for_quiescence, daemon=True)
    waiter.start()
    try:
        with timed() as elapsed:
            ctx.unrelated = 42
        assert elapsed.seconds < PROMPT_SECONDS, elapsed.seconds
        assert ctx.unrelated.value == 42
        if failures:
            raise failures[0]
        assert not barrier_done.is_set(), "the barrier returned before the body could finish"
    finally:
        waiter.join(timeout=60)

    if failures:
        raise failures[0]
    assert barrier_done.is_set()
    assert quiescent(ctx), states(ctx)
