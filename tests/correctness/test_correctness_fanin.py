"""Diamonds — the case §15 A0 asks for, which does not pass now.

§15 A0 asks for expression-correctness tests over "deep and wide dependency
trees — sub-path sources, sub-path targets, chained projections, diamonds",
and says they pass today.  Three of the four do (see
``test_correctness_expressions.py``).  Diamonds do not, and the reason is one
inverted comparison rather than anything about expressions:

``Context._add_edge`` rejects a new edge when ``_would_cycle(source, target)``,
and ``_would_cycle`` walks *forward from the source* and returns true if it
reaches the target.  But adding ``source -> target`` closes a cycle exactly when
the target already reaches the *source*.  The check is the wrong way round, so it
has both failure modes at once:

* **a second edge from one node into another is refused** as a "Dependency
  cycle" — which is what a diamond is, and also what ``tf.pins.x = ctx.a;
  tf.pins.y = ctx.a`` is; and
* **a genuine two-node cycle is accepted** — after ``ctx.b = ctx.a``, writing
  ``ctx.a = ctx.b`` installs the back edge without complaint, leaving the graph
  with a real cycle that only the convergence limit in ``_derive_all`` keeps from
  spinning.

Both directions are asserted here.  The tests are cell-only where they can be,
so they need no execution and can go green as soon as the check is fixed; the
design does not schedule this work, and A2 (topology through class-2 messages)
is where it naturally lands.

Legacy exercised this constantly: ``subsubcell.py`` connects four different
depths of one cell into one transformer, and ``subcell.py`` builds ``ctx.q`` from
four sources at once.
"""

from __future__ import annotations

import pytest

from contract_helpers import states
from seamless_workflow import Context
from seamless_workflow.errors import DependencyError


def add(x, y):
    return x + y


def report(a, b, c, d):
    return [a, b, c, d]


@pytest.mark.a2
def test_two_sub_paths_of_one_cell_feed_one_target_cell():
    ctx = Context()
    ctx.a = {"x": 1, "y": 2}
    ctx.p = {}

    ctx.p.left = ctx.a.x
    ctx.p.right = ctx.a.y

    assert ctx.p.value == {"left": 1, "right": 2}


@pytest.mark.a2
def test_one_source_feeds_two_pins_of_one_transformer():
    ctx = Context()
    ctx.a = 21
    ctx.tf = add

    ctx.tf.pins.x = ctx.a
    ctx.tf.pins.y = ctx.a

    assert ctx.tf.state != "unwired", states(ctx)


@pytest.mark.a2
def test_a_wide_fan_in_from_one_root_is_accepted():
    """Legacy ``subsubcell.py``: four depths of one cell into one transformer."""

    ctx = Context()
    ctx.a = {"b": {"c": {"d": 10}}}
    ctx.report = report

    ctx.report.pins.a = ctx.a
    ctx.report.pins.b = ctx.a.b
    ctx.report.pins.c = ctx.a.b.c
    ctx.report.pins.d = ctx.a.b.c.d

    assert ctx.report.state != "unwired", states(ctx)


@pytest.mark.now
def test_a_diamond_through_two_intermediate_cells_is_accepted():
    """Green today: the two edges arrive from two *different* source nodes.

    Kept in the regression net because it is the shape closest to the ones that
    fail, and fixing the cycle check must not break it.
    """

    ctx = Context()
    ctx.root = {"x": 1, "y": 2}
    ctx.left = ctx.root.x
    ctx.right = ctx.root.y
    ctx.join = {}

    ctx.join.left = ctx.left
    ctx.join.right = ctx.right

    assert ctx.join.value == {"left": 1, "right": 2}


@pytest.mark.a2
def test_a_repeated_edge_between_the_same_pair_is_not_a_cycle():
    """The minimal form of the same defect, with nothing else in the graph."""

    ctx = Context()
    ctx.a = {"x": 1, "y": 2}
    ctx.b = {}
    ctx.b.first = ctx.a.x

    ctx.b.second = ctx.a.y  # same source node, same target node, second edge

    assert ctx.b.value == {"first": 1, "second": 2}


@pytest.mark.a2
def test_a_two_node_cycle_is_still_rejected():
    """The other half of the inverted check: today this is accepted."""

    ctx = Context()
    ctx.a = 1
    ctx.b = ctx.a

    with pytest.raises(DependencyError):
        ctx.a = ctx.b


@pytest.mark.a2
def test_a_three_node_cycle_is_still_rejected():
    ctx = Context()
    ctx.a = 1
    ctx.b = ctx.a
    ctx.c = ctx.b

    with pytest.raises(DependencyError):
        ctx.a = ctx.c


@pytest.mark.now
def test_a_self_dependency_is_still_rejected():
    """The one cycle case the current check does catch, kept as a regression net."""

    ctx = Context()
    ctx.a = {"x": 1}

    with pytest.raises(DependencyError):
        ctx.a.y = ctx.a.x
