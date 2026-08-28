"""§15 A0 — "connect a downstream -> returns promptly; downstream is waiting, not computing".

The second half of the latency script, and the one with the sharper measurement
behind it.  Against the current implementation, connecting one downstream
transformer to a node that has already produced its result **re-executes the
upstream body seven more times** — measured by the counter below, which reads 8
where 1 is correct.  ``_derive_all`` re-derives every node once per convergence
pass and makes up to one pass per node, and derivation *is* execution, so the
cost of adding a node to a settled graph is quadratic in bodies run.  Wall-clock
shows it; the counter proves it.

The counter is a file, not a module-level integer: execution moves to a worker
process at A4, and an integer in the test process would silently stop counting
exactly when the numbers start mattering.  The log path travels as an ordinary
pin, so it is part of the transformation identity — which also means each test
measures a fresh computation rather than a cache hit inherited from an earlier
one.
"""

from __future__ import annotations

import pytest

from contract_helpers import (
    PROMPT_SECONDS,
    SHORT_BODY_SECONDS,
    result_checksum,
    settle,
    state,
    states,
    timed,
)
from seamless_workflow import Context


def logged_add(x, y, delay, log_path):
    import time

    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("run\n")
    if delay:
        time.sleep(delay)
    return x + y


def double(x):
    return 2 * x


@pytest.mark.a1
@pytest.mark.slow
def test_connecting_a_downstream_returns_promptly(execution_log):
    ctx = Context()
    ctx.tf = logged_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.log_path = execution_log.path
    ctx.tf.pins.y = 2

    with timed() as elapsed:
        ctx.tail = double
        ctx.tail.pins.x = ctx.tf

    assert elapsed.seconds < PROMPT_SECONDS, (
        f"connecting a downstream transformer blocked the caller for "
        f"{elapsed.seconds:.1f}s"
    )


@pytest.mark.a1
@pytest.mark.slow
def test_a_freshly_connected_downstream_is_waiting_not_computing(execution_log):
    ctx = Context()
    ctx.tf = logged_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.log_path = execution_log.path
    ctx.tf.pins.y = 2

    ctx.tail = double
    ctx.tail.pins.x = ctx.tf

    assert state(ctx, "tail") == "waiting", states(ctx)
    assert result_checksum(ctx, "tail") is None


@pytest.mark.a4
@pytest.mark.slow
def test_connecting_a_downstream_does_not_re_execute_the_upstream_body(execution_log):
    """One transformation identity, one execution — whatever happens downstream."""

    ctx = Context()
    ctx.tf = logged_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.log_path = execution_log.path

    ctx.tf.pins.y = 2
    assert settle(ctx, timeout=60), states(ctx)
    assert execution_log.count() == 1, execution_log.entries()

    ctx.tail = double
    ctx.tail.pins.x = ctx.tf
    ctx.out = ctx.tail
    assert settle(ctx, timeout=60), states(ctx)

    assert ctx.out.value == 84
    assert execution_log.count() == 1, (
        f"the upstream body ran {execution_log.count()} times; connecting a "
        f"downstream node must not re-execute an upstream that has not changed"
    )


@pytest.mark.a4
@pytest.mark.slow
def test_an_unrelated_node_does_not_re_execute_an_existing_one(execution_log):
    """Adding an unrelated node is not a reason to recompute anything."""

    ctx = Context()
    ctx.tf = logged_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.delay = 0
    ctx.tf.pins.log_path = execution_log.path
    ctx.tf.pins.y = 2
    assert settle(ctx, timeout=60), states(ctx)
    assert execution_log.count() == 1

    ctx.unrelated_cell = {"noise": True}
    ctx.another = double
    ctx.another.pins.x = 5
    assert settle(ctx, timeout=60), states(ctx)

    assert execution_log.count() == 1, execution_log.entries()
