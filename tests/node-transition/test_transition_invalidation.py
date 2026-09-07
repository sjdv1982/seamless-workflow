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

**The negative half.**  "Its *entire* downstream cone" is two claims, and the
positive tests above check only the first.  The second is that nothing *else*
leaves ``complete``: over-invalidation is a defect too, and a quieter one, since
it never produces a wrong answer — only recomputation nobody asked for.  The cone
of an edited node is its transitive consumers, which is narrower than "everything
reachable from a shared ancestor": two consumers of one root are not in each
other's cones, and an input is never in its consumer's cone.

Those negatives need **two different instruments**, because one of them cannot
see the defect at all:

* *state and checksum, before versus after*.  Cheap, and phase-robust — stated as
  **unchanged across the edit** rather than as *still complete*, so it is
  discriminating today and vacuously true in the A1–A3 limbo, where the compared
  nodes are pending on both sides.  This is the regression net.
* *execution count*, through ``seamless_transformer.observation``.  Necessary,
  because a deterministic body makes over-invalidation **invisible** to the first
  instrument: the node is revoked, recomputed, and lands on exactly the checksum
  it had, so the before/after comparison passes while work was thrown away.
  Only counting can tell "never invalidated" from "invalidated and rebuilt".

Measured on the current implementation: editing one literal pin of ``tf2``
re-executes ``island`` — which shares no ancestor with anything in the graph —
three more times.  The checksum comparison notices none of it.
"""

from __future__ import annotations

import pytest

from contract_helpers import (
    PENDING,
    compute_or_settle,
    states,
    try_settle,
)
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
    try_settle(ctx, timeout=30)

    ctx.a = 100

    assert ctx.a.state == "complete"
    for name, node in (("tf1", ctx.tf1), ("tf2", ctx.tf2), ("tf3", ctx.tf3), ("result", ctx.result)):
        assert node.state in PENDING, (name, states(ctx))


@pytest.mark.a1
def test_revocation_reaches_the_whole_cone_not_just_the_first_hop():
    """"the *entire* downstream cone", including a fan-out beyond the first node."""

    ctx = _chain(Context())
    ctx.branch = double
    ctx.branch.pins.x = ctx.tf1
    ctx.branch_out = ctx.branch
    try_settle(ctx, timeout=30)

    ctx.a = 7

    # A transformer's `.result` is a read-only Cell on the same node, so one
    # handle answers for that node's state as well as for its checksum.
    for name, cell in (
        ("tf1", ctx.tf1.result),
        ("tf2", ctx.tf2.result),
        ("tf3", ctx.tf3.result),
        ("result", ctx.result),
        ("branch", ctx.branch.result),
        ("branch_out", ctx.branch_out),
    ):
        assert cell.state in PENDING, (name, states(ctx))
        assert cell.checksum is None, name


@pytest.mark.now
def test_a_topology_change_revokes_the_downstream_cone():
    """Class 2 revokes exactly as class 3 does; ``unwired`` blocks the cone."""

    ctx = _chain(Context())
    try_settle(ctx, timeout=30)

    del ctx.tf1.pins.y

    assert ctx.tf1.state == "unwired"
    for name, cell in (("tf2", ctx.tf2.result), ("tf3", ctx.tf3.result), ("result", ctx.result)):
        assert cell.state not in {"complete"}, (name, states(ctx))
        assert cell.checksum is None, name


@pytest.mark.a1
def test_invalidation_is_not_deferred_to_a_barrier():
    """The revocation is part of the writing turn, not of a later wait.

    No barrier is called anywhere in this test: if revocation only happened
    once someone asked for a result, a reader that never asks would keep
    observing the stale one.
    """

    ctx = _chain(Context())
    try_settle(ctx, timeout=30)

    ctx.a = 5

    assert "complete" not in {
        node.state for node in (ctx.tf1, ctx.tf2, ctx.tf3, ctx.result)
    }


@pytest.mark.a4
def test_a_stale_result_is_never_observable_after_an_upstream_edit():
    """The same property stated over checksums rather than over states."""

    ctx = _chain(Context())
    compute_or_settle(ctx)

    cone = (
        ("tf1", ctx.tf1.result),
        ("tf2", ctx.tf2.result),
        ("tf3", ctx.tf3.result),
        ("result", ctx.result),
    )
    stale = {name: cell.checksum for name, cell in cone}
    assert all(value is not None for value in stale.values())
    assert ctx.result.value == -((1 + 2) * 2)

    ctx.a = 10

    for name, cell in cone:
        # `is None` rather than `!= stale[name]`: Checksum.__eq__ constructs its
        # operand, and Checksum(None) raises, so the revoked case cannot be
        # stated as an inequality against a checksum that is still there.
        assert cell.checksum is None, (name, stale[name])

    compute_or_settle(ctx)
    assert ctx.result.value == -((10 + 2) * 2)


# --------------------------------------------------------------- the negatives


def _branched(ctx):
    """One shared root with two independent consumers, plus a disconnected island.

    ::

        a ──> tf1 ──> tf2 ──> result       tf2 also takes a literal pin
        └───> sibling

        island                             shares no ancestor with anything

    Three different "outside the cone" relationships in one graph: ``sibling`` is
    a *co-consumer* of the edited node's input, ``tf1`` and ``a`` are *upstream*
    of it, and ``island`` is *unrelated*.  Each is a distinct way to be outside,
    and an implementation can get one right and the others wrong — over-eager
    revocation usually walks the shared ancestor, which catches ``sibling`` and
    ``a`` but not ``island``, while a whole-graph sweep catches all three.
    """

    ctx.a = 1
    ctx.tf1 = add
    ctx.tf1.pins.x = ctx.a
    ctx.tf1.pins.y = 2
    ctx.tf2 = add
    ctx.tf2.pins.x = ctx.tf1
    ctx.tf2.pins.y = 10
    ctx.result = ctx.tf2
    ctx.sibling = negate
    ctx.sibling.pins.x = ctx.a
    ctx.island = double
    ctx.island.pins.x = 5
    return ctx


def _snapshot(pairs):
    """``{name: (state, checksum-hex-or-None)}`` — comparable across an edit.

    Checksums are reduced to hex or ``None`` because ``Checksum.__eq__``
    constructs its operand and ``Checksum(None)`` raises, so a node that has a
    checksum on one side of the edit and not on the other cannot be compared
    directly.
    """

    snapshot = {}
    for name, handle in pairs:
        checksum = handle.checksum
        snapshot[name] = (handle.state, checksum.hex() if checksum is not None else None)
    return snapshot


def _outside(ctx):
    return (
        ("a", ctx.a),
        ("tf1", ctx.tf1.result),
        ("sibling", ctx.sibling.result),
        ("island", ctx.island.result),
    )


@pytest.mark.now
def test_an_edit_leaves_every_node_outside_the_cone_untouched():
    """The cone of ``tf2`` is ``{tf2, result}`` and nothing else.

    ``a`` and ``tf1`` are upstream of the edited node, ``sibling`` consumes the
    same root without depending on ``tf2``, and ``island`` is unrelated.  None of
    them is a consumer of ``tf2``, so none of them may move.
    """

    ctx = _branched(Context())
    try_settle(ctx, timeout=30)
    before = _snapshot(_outside(ctx))

    ctx.tf2.pins.y = 99

    after = _snapshot(_outside(ctx))
    assert after == before, {
        name: (before[name], after[name])
        for name in before
        if before[name] != after[name]
    }


@pytest.mark.now
def test_an_edit_of_the_shared_root_still_spares_the_unrelated_subgraph():
    """The complement of the positive fan-out test.

    Editing ``a`` legitimately revokes ``tf1``, ``tf2``, ``result`` *and*
    ``sibling`` — every consumer of ``a``.  ``island`` consumes nothing that
    ``a`` reaches, so the widest legitimate revocation in this graph must still
    stop before it.
    """

    ctx = _branched(Context())
    try_settle(ctx, timeout=30)
    before = _snapshot((("island", ctx.island.result),))

    ctx.a = 100

    assert _snapshot((("island", ctx.island.result),)) == before


@pytest.mark.now
def test_a_topology_change_leaves_nodes_outside_the_cone_untouched():
    """Class 2, stated negatively, mirroring the positive test above.

    Disconnecting a pin of ``tf1`` makes ``tf1`` ``unwired`` and blocks its
    consumers.  It says nothing about ``tf1``'s own input, about a co-consumer of
    that input, or about an unrelated node — a revocation that walked edges in
    the wrong direction would take ``a``, and one that swept the graph would take
    all three.
    """

    ctx = _branched(Context())
    try_settle(ctx, timeout=30)
    spared = (("a", ctx.a), ("sibling", ctx.sibling.result), ("island", ctx.island.result))
    before = _snapshot(spared)

    del ctx.tf1.pins.y

    assert ctx.tf1.state == "unwired"
    assert ctx.tf2.state not in {"complete"}, states(ctx)
    after = _snapshot(spared)
    assert after == before, {
        name: (before[name], after[name])
        for name in before
        if before[name] != after[name]
    }


@pytest.mark.a4
def test_an_edit_does_not_re_execute_anything_outside_the_cone(
    transformation_observations,
):
    """The half the checksum comparison cannot see.

    A deterministic body makes over-invalidation invisible to state and
    checksum: revoke a node, recompute it, and it lands on exactly the checksum
    it had, so the test above passes while the work was thrown away.  Counting
    executions is the only way to tell "never invalidated" from "invalidated and
    rebuilt", and it is what turns the negative from a statement about
    *observability* into one about *cost*.

    Measured today: editing one literal pin of ``tf2`` re-runs ``island`` three
    more times, and ``sibling`` three more times.  Neither is a consumer of
    ``tf2``; ``island`` is not a consumer of anything in the graph.
    """

    observations = transformation_observations

    ctx = _branched(Context())
    try_settle(ctx, timeout=30)
    before = {
        name: len(observations.misses(name)) for name in ("tf1", "sibling", "island")
    }

    ctx.tf2.pins.y = 99
    try_settle(ctx, timeout=30)

    after = {
        name: len(observations.misses(name)) for name in ("tf1", "sibling", "island")
    }
    assert after == before, (
        f"editing tf2 re-executed nodes outside its cone: "
        f"{ {name: (before[name], after[name]) for name in before if before[name] != after[name]} }"
    )
