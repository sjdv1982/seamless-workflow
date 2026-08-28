"""§14.1 — a turn may not deliver a result that has not been computed yet.

    A message that makes a node newly eligible to compute leaves it pending in
    that turn.  Its result checksum — or its failure — is settled by a later
    turn, driven by a class-5 notification.  No public operation returns a
    freshly computed result.

**What ``waiting`` means here** (§24.5, which requires this to be written down
before the first transition test, "which will otherwise encode its author's
reading").  This suite encodes reading **(a)**, the one the design recommends:

* ``waiting``   — the node's inputs are not all concrete checksums yet.
* ``computing`` — the node's inputs are concrete and its work has been
  submitted, whether it is queued behind backend concurrency or already running.

Reading (a) is knowable at dispatch; reading (b) ("not yet executing") requires
the backend to report start-of-execution, which jobserver and Dask may not
surface.  The visible consequence, which the tests below assert directly: a
transformer whose pins are all literals goes straight to ``computing`` and never
shows ``waiting``; only its downstream shows ``waiting``.  The §15 A0 latency
script assumes the same thing ("set the last pin -> state is computing").

If the project later settles on reading (b), the assertions to flip are the
``computing`` ones in :func:`test_a_transformer_with_concrete_inputs_is_computing`
and :func:`test_the_whole_downstream_cone_is_pending`; everything else in this
file is stated as "pending, and no result checksum", which holds under both.

Assertions are on internal node state, never on ``.status``: ``_PUBLIC_STATUS``
maps ``waiting`` and ``computing`` to the same string.
"""

from __future__ import annotations

import pytest

from contract_helpers import PENDING, result_checksum, state, states
from seamless_workflow import Context


def add(x, y):
    return x + y


def double(x):
    return 2 * x


def negate(x):
    return -x


@pytest.mark.a1
def test_setting_the_last_pin_does_not_deliver_a_result():
    """The defining case of §14.1: eligibility is not delivery."""

    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 10
    assert state(ctx, "tf") == "unwired"

    ctx.tf.pins.y = 32

    assert state(ctx, "tf") in PENDING, states(ctx)
    assert result_checksum(ctx, "tf") is None
    assert ctx.tf.result.checksum is None
    assert ctx.tf.result.value is None


@pytest.mark.a1
def test_a_transformer_with_concrete_inputs_is_computing():
    """Reading (a): concrete inputs mean submitted work, not waiting for inputs."""

    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.tf.pins.y = 2

    assert state(ctx, "tf") == "computing", states(ctx)


@pytest.mark.a1
def test_a_transformer_awaiting_an_upstream_result_is_waiting():
    """Reading (a): a node whose input is still a promise is ``waiting``."""

    ctx = Context()
    ctx.head = add
    ctx.head.pins.x = 1
    ctx.head.pins.y = 2
    ctx.tail = double
    ctx.tail.pins.x = ctx.head

    assert state(ctx, "head") == "computing", states(ctx)
    assert state(ctx, "tail") == "waiting", states(ctx)
    assert result_checksum(ctx, "tail") is None


@pytest.mark.a1
def test_the_whole_downstream_cone_is_pending():
    """One eligible node, and everything below it, settle in later turns."""

    ctx = Context()
    ctx.tf1 = add
    ctx.tf2 = double
    ctx.tf3 = negate
    ctx.tf2.pins.x = ctx.tf1
    ctx.tf3.pins.x = ctx.tf2
    ctx.result = ctx.tf3
    ctx.tf1.pins.x = 1

    assert state(ctx, "tf1") == "unwired"

    ctx.tf1.pins.y = 2  # the last missing pin of the whole cone

    assert states(ctx) == {
        "result": "waiting",
        "tf1": "computing",
        "tf2": "waiting",
        "tf3": "waiting",
    }
    for path in ("tf1", "tf2", "tf3", "result"):
        assert result_checksum(ctx, path) is None, path


@pytest.mark.a1
def test_a_cell_fed_by_a_pending_transformer_is_waiting():
    """An alias edge forwards a checksum; there is no checksum to forward yet."""

    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 3
    ctx.tf.pins.y = 4
    ctx.out = ctx.tf

    assert state(ctx, "out") == "waiting", states(ctx)
    assert ctx.out.value is None
    assert ctx.out.checksum is None


@pytest.mark.now
def test_pure_propagation_still_completes_in_turn():
    """§14.1's explicit exception, and the guard against over-correcting at A1.

    Installing a written checksum and forwarding one across an alias edge
    compute nothing, so they must keep completing inside the turn.  A1 must
    break synchronicity for *computation* only.
    """

    ctx = Context()
    ctx.a = {"x": 1}
    ctx.b = ctx.a
    ctx.c = ctx.b

    assert states(ctx) == {"a": "complete", "b": "complete", "c": "complete"}
    assert ctx.c.value == {"x": 1}

    ctx.a = {"x": 2}
    assert states(ctx) == {"a": "complete", "b": "complete", "c": "complete"}
    assert ctx.c.value == {"x": 2}

    ctx.a.x = 3  # sub-path write: still no computation
    assert state(ctx, "c") == "complete"
    assert ctx.c.value == {"x": 3}


@pytest.mark.now
def test_public_status_cannot_distinguish_waiting_from_computing():
    """Why this suite asserts internal state (§15 A0).

    Kept as a test rather than a comment so that a future split of the public
    status strings makes this file, and the reason it is written this way,
    visible.
    """

    from seamless_workflow.builder_state import _PUBLIC_STATUS

    assert _PUBLIC_STATUS["waiting"] == _PUBLIC_STATUS["computing"] == "Status: pending"
