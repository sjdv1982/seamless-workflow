"""§15 A0 — "a disconnected required pin yields ``unwired`` + ``blocked-by-unwired`` downstream".

``unwired`` needs no execution to reach, so most of this file describes current
behaviour and is a regression net.  One case does not hold today and is the
reason the file exists: a **transformer** downstream of an ``unwired``
transformer is reported ``blocked-by-error``, while a **cell** in the same
position is correctly reported ``blocked-by-unwired``.  The two paths disagree
because ``_apply_pending`` folds every non-pending upstream state that is not
``failed``/``blocked`` into the error reason, whereas ``_apply_upstream_state``
distinguishes them.

The distinction is not cosmetic: ``blocked-by-error`` tells a user to look for
an exception that does not exist, on a graph whose actual defect is a missing
connection.
"""

from __future__ import annotations

import pytest

from contract_helpers import block_reason, result_checksum, state, states
from seamless_workflow import Context


def add(x, y):
    return x + y


def double(x):
    return 2 * x


def optional_sum(x, y=None):
    return x if y is None else x + y


@pytest.mark.now
def test_a_missing_required_pin_leaves_the_transformer_unwired():
    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1

    assert state(ctx, "tf") == "unwired"
    assert block_reason(ctx, "tf") is None
    assert result_checksum(ctx, "tf") is None


@pytest.mark.now
def test_a_cell_downstream_of_an_unwired_transformer_is_blocked_by_unwired():
    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.out = ctx.tf

    assert state(ctx, "out") == "blocked"
    assert block_reason(ctx, "out") == "blocked-by-unwired"


@pytest.mark.a1
def test_a_transformer_downstream_of_an_unwired_transformer_is_blocked_by_unwired():
    """Fails today: reported ``blocked-by-error`` with no error anywhere."""

    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.tf

    assert state(ctx, "tail") == "blocked", states(ctx)
    assert block_reason(ctx, "tail") == "blocked-by-unwired"
    assert ctx.tail.exception is None


@pytest.mark.a1
def test_the_unwired_reason_survives_a_second_hop():
    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.mid = double
    ctx.mid.pins.x = ctx.tf
    ctx.tail = double
    ctx.tail.pins.x = ctx.mid

    assert block_reason(ctx, "mid") == "blocked-by-unwired", states(ctx)
    assert block_reason(ctx, "tail") == "blocked-by-unwired", states(ctx)


@pytest.mark.now
def test_deleting_a_required_pin_returns_a_transformer_to_unwired():
    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.tf.pins.y = 2
    assert state(ctx, "tf") != "unwired"

    del ctx.tf.pins.y

    assert state(ctx, "tf") == "unwired"
    assert result_checksum(ctx, "tf") is None


@pytest.mark.now
def test_an_optional_pin_left_unset_does_not_make_a_transformer_unwired():
    ctx = Context()
    ctx.tf = optional_sum
    ctx.tf.optional_pins = {"y"}
    ctx.tf.pins.x = 5

    assert state(ctx, "tf") != "unwired", states(ctx)
