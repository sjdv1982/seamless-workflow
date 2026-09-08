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

Assertions are on ``.state``, the node's own six-state vocabulary.  The public
``.status`` string that used to map ``waiting`` and ``computing`` both onto
``"Status: pending"`` is gone; see the last test in this file.
"""

from __future__ import annotations

from typing import get_args

import pytest

from contract_helpers import PENDING, states
from seamless import ProjectionError
from seamless_workflow import Context
from seamless_workflow.graph import NodeState


def add(x, y):
    import time
    time.sleep(0.5)
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
    assert ctx.tf.state == "unwired"

    ctx.tf.pins.y = 32

    assert ctx.tf.state in PENDING, states(ctx)
    assert ctx.tf.result.checksum is None
    assert ctx.tf.result.value is None


@pytest.mark.a1
def test_a_transformer_with_concrete_inputs_is_computing():
    """Reading (a): concrete inputs mean submitted work, not waiting for inputs."""

    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.tf.pins.y = 2

    assert ctx.tf.state == "computing", states(ctx)


@pytest.mark.a1
def test_a_transformer_awaiting_an_upstream_result_is_waiting():
    """Reading (a): a node whose input is still a promise is ``waiting``."""

    ctx = Context()
    ctx.head = add
    ctx.head.pins.x = 1
    ctx.head.pins.y = 2
    ctx.tail = double
    ctx.tail.pins.x = ctx.head

    assert ctx.head.state == "computing", states(ctx)
    assert ctx.tail.state == "waiting", states(ctx)
    assert ctx.tail.result.checksum is None


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

    assert ctx.tf1.state == "unwired"

    ctx.tf1.pins.y = 2  # the last missing pin of the whole cone

    assert states(ctx) == {
        "result": "waiting",
        "tf1": "computing",
        "tf2": "waiting",
        "tf3": "waiting",
    }
    # A transformer's `.result` is a read-only Cell on the same node, so one
    # handle answers for that node's checksum as well as for its state.
    for name, cell in (
        ("tf1", ctx.tf1.result),
        ("tf2", ctx.tf2.result),
        ("tf3", ctx.tf3.result),
        ("result", ctx.result),
    ):
        assert cell.checksum is None, name


@pytest.mark.a1
def test_a_cell_fed_by_a_pending_transformer_is_waiting():
    """An alias edge forwards a checksum; there is no checksum to forward yet."""

    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 3
    ctx.tf.pins.y = 4
    ctx.out = ctx.tf

    assert ctx.out.state == "waiting", states(ctx)
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
    assert ctx.c.state == "complete"
    assert ctx.c.value == {"x": 3}


@pytest.mark.now
def test_no_public_string_collapses_waiting_and_computing():
    """Why this suite asserts ``.state`` (§15 A0), kept as a test rather than a comment.

    ``.status`` mapped ``waiting`` and ``computing`` both onto
    ``"Status: pending"``, which is why §15 A0 tells transition tests not to
    assert on it.  Rather than work around that string, the project removed it:
    ``.state`` and ``.block_reason`` carry strictly more than it did (six states
    and two block reasons against five strings), and a display summary lives in
    the handle's ``repr``.

    This test guards the removal in both directions -- the name must stay gone,
    and the two states must stay distinguishable -- so that reintroducing a
    collapsing status string fails here, in the file whose subject it is.
    """

    ctx = Context()
    ctx.a = 1
    ctx.tf = add

    # Gone from the Transformer, which has no projection fallback, so Python
    # reports it directly -- and suggests ``state`` while it is at it.
    with pytest.raises(AttributeError):
        ctx.tf.status

    # Gone from the Cell too.  There the name still *resolves*, to a sub-path
    # projection of the cell's value, so removal alone would have made
    # ``ctx.a.status == ...`` silently unequal rather than wrong.  SubCell is
    # what makes it loud; this is the assertion that the two changes belong
    # together.
    with pytest.raises(ProjectionError):
        ctx.a.status == "Status: OK"

    # And the mapping the string came from is gone with it.
    with pytest.raises(ImportError):
        from seamless_workflow.builder_state import _PUBLIC_STATUS  # noqa: F401

    # What replaced it keeps the two states the string collapsed.
    assert {"waiting", "computing"} <= set(get_args(NodeState))
