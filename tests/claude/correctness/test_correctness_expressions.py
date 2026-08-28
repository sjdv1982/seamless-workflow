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

from contract_helpers import state, states
from seamless_workflow import Context


@pytest.mark.now
def test_a_sub_path_source_projects_one_key():
    ctx = Context()
    ctx.a = {"x": 1, "y": 2}
    ctx.b = ctx.a.x

    assert ctx.b.value == 1
    assert state(ctx, "b") == "complete"


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


@pytest.mark.a3
def test_a_missing_key_is_distinguishable_from_a_key_holding_null():
    """[MOD-3]: a sub-path read becomes a real expression evaluation.

    Today both cases are reported ``complete`` with value ``None``, so a typo in
    a projection path is indistinguishable from a deliberate null — the failure
    mode a content-addressed graph is least able to recover from later, because
    downstream consumes a well-formed ``None``.

    The assertion deliberately does not choose the answer (``failed``, a distinct
    block reason, or an exception on read).  It requires only that the two cases
    stop being the same.
    """

    ctx = Context()
    ctx.a = {"present": None}
    ctx.present = ctx.a.present
    ctx.missing = ctx.a.absent

    present = (state(ctx, "present"), ctx.present.value, ctx.present.checksum)
    missing = (state(ctx, "missing"), ctx.missing.value, ctx.missing.checksum)

    assert present != missing, (present, states(ctx))
