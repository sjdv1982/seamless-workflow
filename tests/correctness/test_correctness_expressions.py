"""§15 A0 — expression correctness over Cell-only Contexts.

    Write expression-correctness tests over Cell-only Contexts with deep and
    wide dependency trees — sub-path sources, sub-path targets, chained
    projections, diamonds.  These pass now and must keep passing; they are the
    regression net for [MOD-3].

[MOD-3] deletes the Context's private sub-path evaluator — which today resolves
the parent buffer, indexes the Python value and re-hashes it with the *parent's*
celltype (``context.py:1034-1041``) — and replaces it with
``seamless.checksum.expression``.  That is a net deletion of Context code, and
the risk of a deletion is silent behaviour change.  Hence this file: it is
written against behaviour, not against the implementation, so it keeps meaning
after the evaluator is gone.

Cell-only on purpose: no transformer, so nothing here depends on execution and
the whole file must stay green through the A1–A3 limbo.

Diamonds live in ``test_correctness_fanin.py`` — they turn out not to pass now.
"""

from __future__ import annotations

import pytest

from contract_helpers import states
from seamless_workflow import Context


@pytest.mark.now
def test_a_sub_path_source_projects_one_key():
    ctx = Context()
    ctx.a = {"x": 1, "y": 2}
    ctx.b = ctx.a.x

    assert ctx.b.value == 1
    assert ctx.b.state == "complete"


@pytest.mark.now
def test_a_deep_sub_path_source_projects_through_levels():
    ctx = Context()
    ctx.a = {"y": {"z": {"deep": 7}}}
    ctx.b = ctx.a.y.z.deep

    assert ctx.b.value == 7


@pytest.mark.now
def test_a_sub_path_target_merges_into_a_container():
    ctx = Context()
    ctx.src = 5
    ctx.target = {}
    ctx.target.k = ctx.src

    assert ctx.target.value == {"k": 5}


@pytest.mark.now
def test_a_chained_projection_reads_through_an_intermediate_node():
    ctx = Context()
    ctx.a = {"y": {"z": 2}}
    ctx.mid = {}
    ctx.mid.inner = ctx.a.y
    ctx.leaf = ctx.mid.inner.z

    assert ctx.mid.value == {"inner": {"z": 2}}
    assert ctx.leaf.value == 2


@pytest.mark.now
def test_projections_follow_an_edit_of_the_root():
    ctx = Context()
    ctx.a = {"x": 1, "y": {"z": 2}}
    ctx.x = ctx.a.x
    ctx.z = ctx.a.y.z

    ctx.a = {"x": 10, "y": {"z": 20}}

    assert ctx.x.value == 10
    assert ctx.z.value == 20


@pytest.mark.now
def test_projections_follow_a_sub_path_edit_of_the_root():
    ctx = Context()
    ctx.a = {"x": 1, "y": {"z": 2}}
    ctx.z = ctx.a.y.z

    ctx.a.y = {"z": 99}

    assert ctx.z.value == 99


@pytest.mark.now
def test_a_deep_tree_of_projections_stays_consistent():
    """Legacy ``subsubcell.py`` shape, cell-only: four depths of one root."""

    ctx = Context()
    ctx.a = {"b": {"c": {"d": 10}}}
    ctx.level_b = ctx.a.b
    ctx.level_c = ctx.a.b.c
    ctx.level_d = ctx.a.b.c.d

    assert ctx.level_b.value == {"c": {"d": 10}}
    assert ctx.level_c.value == {"d": 10}
    assert ctx.level_d.value == 10

    ctx.a.b.c.d = 999

    assert ctx.level_b.value == {"c": {"d": 999}}
    assert ctx.level_c.value == {"d": 999}
    assert ctx.level_d.value == 999


@pytest.mark.now
def test_a_wide_tree_of_projections_stays_independent():
    ctx = Context()
    ctx.a = {"one": 1, "two": 2, "three": 3}
    ctx.one = ctx.a.one
    ctx.two = ctx.a.two
    ctx.three = ctx.a.three

    ctx.a.two = 22

    assert (ctx.one.value, ctx.two.value, ctx.three.value) == (1, 22, 3)


@pytest.mark.now
def test_a_projection_of_a_container_is_a_copy_not_an_alias():
    ctx = Context()
    ctx.a = {"y": {"z": 2}}
    ctx.y = ctx.a.y

    projected = ctx.y.value
    projected["z"] = 3

    assert ctx.y.value == {"z": 2}
    assert ctx.a.value == {"y": {"z": 2}}


def _missing_key_graph():
    ctx = Context()
    ctx.a = {"present": None}
    ctx.present = ctx.a.present
    ctx.missing = ctx.a.absent
    return ctx


@pytest.mark.now
def test_a_missing_key_is_distinguishable_from_a_key_holding_null():
    """The two cases differ — but only in the checksum, and only if you look there.

    ``state`` and ``.value`` are identical (``complete``, ``None``) for both.
    The one field that separates them is the node checksum: the present-but-null
    key carries the checksum of ``null``, the absent key carries none at all.
    [MOD-3] must not collapse that distinction while moving sub-path reads onto
    ``evaluate_expression``; this is the regression net for it, not a statement
    that the current answer is a good one — see the ``a3`` test below.
    """

    ctx = _missing_key_graph()

    def fields(cell):
        # Checksums are compared as hex or ``None``, never as ``Checksum``
        # objects: ``Checksum.__eq__`` constructs its operand and
        # ``Checksum(None)`` raises ``TypeError``, so a tuple comparison where
        # one side holds a checksum and the other holds ``None`` errors out
        # instead of answering the question the test is asking.
        checksum = cell.checksum
        return cell.state, cell.value, checksum.hex() if checksum is not None else None

    present = fields(ctx.present)
    missing = fields(ctx.missing)

    assert present != missing, (present, states(ctx))
    assert present[:2] == missing[:2], (
        "the state and value are expected to be identical; if they stop being so, "
        "this test has become weaker than it looks and the assertion above no "
        "longer proves what it says"
    )


@pytest.mark.a3
def test_a_missing_key_is_reported_as_something_other_than_complete():
    """[MOD-3]: how the two differ is the defect, not whether they differ.

    A typo in a projection path produces a node that reports ``complete`` while
    holding no checksum — the invariant
    ``test_correctness_no_code.py::test_no_node_in_a_graph_is_complete_without_a_result_checksum``
    states over a whole graph, here reached without any transformer at all.  A
    reader who checks ``.state`` or ``.value`` sees a well-formed null.

    The consequence is one hop down and is worse than the cause: a transformer
    whose pin is fed by the typo'd projection reports ``unwired`` — *a pin is not
    connected* — when the pin is connected and it is the key behind it that does
    not exist.  So the diagnosis points at the wiring, which is correct, while
    the graph says the wiring is absent, which is false.

    The assertion deliberately does not choose the answer (``failed``, a distinct
    block reason, or an exception on read).  It requires only that a node with no
    checksum stop calling itself ``complete``.  [MOD-3] moves sub-path reads onto
    ``evaluate_expression``, which is where the distinction can be made.
    """

    ctx = _missing_key_graph()

    assert not (
        ctx.missing.state == "complete" and ctx.missing.checksum is None
    ), (
        f"the projection of an absent key reports {ctx.missing.state!r} with no "
        f"checksum and value {ctx.missing.value!r}"
    )
