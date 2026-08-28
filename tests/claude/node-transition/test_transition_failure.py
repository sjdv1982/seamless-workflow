"""§15 A0 — "a failure yields ``failed`` + ``blocked-by-error`` downstream".

**Phase note, and a correction to the plan.**  §15 lists this among the
node-transition tests that pass at A1.  It cannot: A1 removes in-cascade
execution, and execution is the only producer of ``failed`` in a Context.  A
cell-level failure would not help — cell validators are not applied, an alias
edge does not enforce the target celltype, and the two remaining paths that
could fail a cell (a deeper-than-one-component target, a merge that cannot
serialise) raise ``PathError``/``HashTypeValidationError`` out of the public call
instead of recording a node failure.  So during the A1–A3 limbo no node reaches
``failed`` at all, and these tests are marked ``a4``, when execution — and with
it failure — returns.

**A second defect, found by writing these tests.**  The block *reason* does not
survive a second hop, and it degrades in the opposite direction to the one in
``test_transition_unwired.py``: a cell fed by a node that is already ``blocked``
is reported ``blocked-by-unwired``, because ``_apply_upstream_state`` folds
``blocked`` and ``unwired`` into one branch.  So a graph with a failing
transformer reports *error* one hop down and *missing connection* two hops down,
while a graph with a disconnected pin reports the opposite.  Between them the two
propagation paths manage to be wrong in both directions.

Ported from legacy ``workflow/cascade.py`` (make a transformer invalid, then
valid again, and check the downstream is not left poisoned) and
``workflow/simple-missing.py`` (a downstream reports *upstream*, not an error of
its own).  Legacy asserted these by printing a status dict; here they are
assertions on node state.
"""

from __future__ import annotations

import pytest

from contract_helpers import block_reason, result_checksum, settle, state, states
from seamless_workflow import Context


def boom(x):
    raise RuntimeError("boom")


def double(x):
    return 2 * x


def reciprocal(x):
    return 1 / x


@pytest.mark.a4
def test_a_failing_transformer_is_failed_and_its_cone_is_blocked_by_error():
    ctx = Context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail
    ctx.out = ctx.tail

    assert settle(ctx, timeout=30), states(ctx)

    assert state(ctx, "fail") == "failed"
    assert result_checksum(ctx, "fail") is None
    for path in ("tail", "out"):
        assert state(ctx, path) == "blocked", (path, states(ctx))
        assert block_reason(ctx, path) == "blocked-by-error", path
        assert result_checksum(ctx, path) is None, path


@pytest.mark.a4
def test_the_error_reason_survives_a_second_hop():
    """``blocked-by-error`` must not become ``blocked-by-unwired`` further down."""

    ctx = Context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail
    ctx.out = ctx.tail
    ctx.further = double
    ctx.further.pins.x = ctx.out

    assert settle(ctx, timeout=30), states(ctx)

    assert block_reason(ctx, "tail") == "blocked-by-error", states(ctx)
    assert block_reason(ctx, "out") == "blocked-by-error", states(ctx)
    assert block_reason(ctx, "further") == "blocked-by-error", states(ctx)


@pytest.mark.a4
def test_only_the_failing_node_reports_the_exception():
    """Legacy ``simple-missing.py``: downstream says *upstream*, not *error*."""

    ctx = Context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail

    assert settle(ctx, timeout=30), states(ctx)

    assert isinstance(ctx.fail.exception, BaseException)
    assert "boom" in str(ctx.fail.exception)
    assert ctx.tail.exception is None
    assert ctx.fail.status == "Status: error"
    assert ctx.tail.status == "Status: upstream"


@pytest.mark.a4
def test_a_failure_does_not_survive_the_edit_that_repairs_it():
    """Legacy ``cascade.py``: invalid, then valid again, with no residue."""

    ctx = Context()
    ctx.tf = reciprocal
    ctx.tf.pins.x = 0
    ctx.tail = double
    ctx.tail.pins.x = ctx.tf

    assert settle(ctx, timeout=30), states(ctx)
    assert state(ctx, "tf") == "failed"
    assert state(ctx, "tail") == "blocked"

    ctx.tf.pins.x = 4

    assert settle(ctx, timeout=30), states(ctx)
    assert state(ctx, "tf") == "complete"
    assert state(ctx, "tail") == "complete"
    assert ctx.tf.exception is None
    assert ctx.tail.result.value == 0.5


@pytest.mark.a4
def test_a_repaired_failure_revokes_before_it_recomputes():
    """The repair is itself a class-3 write, so §14.1 applies to it too."""

    ctx = Context()
    ctx.tf = reciprocal
    ctx.tf.pins.x = 0
    ctx.tail = double
    ctx.tail.pins.x = ctx.tf
    assert settle(ctx, timeout=30), states(ctx)

    ctx.tf.pins.x = 4

    assert state(ctx, "tf") != "complete", states(ctx)
    assert state(ctx, "tail") != "complete", states(ctx)
