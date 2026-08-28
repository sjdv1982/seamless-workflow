"""§8/§14.1 — the invalidation half of the contract.

    When a node leaves ``complete``, its entire downstream cone leaves
    ``complete`` in the same non-yielding pass, before anything is evaluated for
    firing.  A downstream node must never be observable as ``complete`` on a
    stale input.

Glitch-freedom itself cannot be tested at A0 (§15 A0: "synchronously it is
trivially true and unobservable"; it becomes an A1 deliverable).  What *is*
testable now is the observable consequence: when a public call that revokes a
value returns, no downstream node is still ``complete``, and no stale result
checksum is readable from one.

The tests are split by what they need.  The three marked ``a1`` never require a
transformer to have produced a result, so they hold during the A1–A3 limbo as
well — vacuously, since nothing is ``complete`` there.  The one marked ``a4``
requires a settled cone first and only becomes discriminating when execution
returns.
"""

from __future__ import annotations

import pytest

from contract_helpers import PENDING, result_checksum, settle, state, states
from seamless_workflow import Context


def add(x, y):
    return x + y


def double(x):
    return 2 * x


def negate(x):
    return -x


def _chain(ctx):
    """a -> tf1 -> tf2 -> tf3 -> result, with tf1 also taking a literal pin."""

    ctx.a = 1
    ctx.tf1 = add
    ctx.tf1.pins.x = ctx.a
    ctx.tf1.pins.y = 2
    ctx.tf2 = double
    ctx.tf2.pins.x = ctx.tf1
    ctx.tf3 = negate
    ctx.tf3.pins.x = ctx.tf2
    ctx.result = ctx.tf3
    return ctx


@pytest.mark.a1
def test_an_upstream_edit_leaves_no_downstream_node_complete():
    ctx = _chain(Context())
    settle(ctx, timeout=30)

    ctx.a = 100

    assert state(ctx, "a") == "complete"
    for path in ("tf1", "tf2", "tf3", "result"):
        assert state(ctx, path) in PENDING, (path, states(ctx))


@pytest.mark.a1
def test_revocation_reaches_the_whole_cone_not_just_the_first_hop():
    """"the *entire* downstream cone", including a fan-out beyond the first node."""

    ctx = _chain(Context())
    ctx.branch = double
    ctx.branch.pins.x = ctx.tf1
    ctx.branch_out = ctx.branch
    settle(ctx, timeout=30)

    ctx.a = 7

    for path in ("tf1", "tf2", "tf3", "result", "branch", "branch_out"):
        assert state(ctx, path) in PENDING, (path, states(ctx))
        assert result_checksum(ctx, path) is None, path


@pytest.mark.now
def test_a_topology_change_revokes_the_downstream_cone():
    """Class 2 revokes exactly as class 3 does; ``unwired`` blocks the cone."""

    ctx = _chain(Context())
    settle(ctx, timeout=30)

    del ctx.tf1.pins.y

    assert state(ctx, "tf1") == "unwired"
    for path in ("tf2", "tf3", "result"):
        assert state(ctx, path) not in {"complete"}, (path, states(ctx))
        assert result_checksum(ctx, path) is None, path


@pytest.mark.a1
def test_invalidation_is_not_deferred_to_a_barrier():
    """The revocation is part of the writing turn, not of a later wait.

    No barrier is called anywhere in this test: if revocation only happened
    once someone asked for a result, a reader that never asks would keep
    observing the stale one.
    """

    ctx = _chain(Context())
    settle(ctx, timeout=30)

    ctx.a = 5

    assert "complete" not in {state(ctx, path) for path in ("tf1", "tf2", "tf3", "result")}


@pytest.mark.a4
def test_a_stale_result_is_never_observable_after_an_upstream_edit():
    """The same property stated over checksums rather than over states."""

    ctx = _chain(Context())
    assert settle(ctx, timeout=30), states(ctx)

    stale = {path: result_checksum(ctx, path) for path in ("tf1", "tf2", "tf3", "result")}
    assert all(value is not None for value in stale.values())
    assert ctx.result.value == -((1 + 2) * 2)

    ctx.a = 10

    for path, checksum in stale.items():
        assert result_checksum(ctx, path) != checksum, path
        assert result_checksum(ctx, path) is None, path

    assert settle(ctx, timeout=30), states(ctx)
    assert ctx.result.value == -((10 + 2) * 2)
