from __future__ import annotations

import pytest

from seamless import Cell
from seamless_workflow import Context
from seamless_workflow.errors import AuthorityError, PathError


def test_literal_assignment_stores_checksum_and_value_copy():
    ctx = Context()
    payload = {"x": [1]}
    ctx.a = payload

    assert ctx.a.checksum is not None
    assert ctx.a.value == {"x": [1]}

    resolved = ctx.a.value
    resolved["x"].append(2)
    assert ctx.a.value == {"x": [1]}


def test_bound_alias_survives_assignment():
    ctx = Context()
    cell = Cell({"a": 1}, celltype="mixed")

    ctx.a = cell
    assert cell.value == {"a": 1}

    ctx.a = {"a": 2}
    assert cell.value == {"a": 2}
    cell.set({"a": 3})
    assert ctx.a.value == {"a": 3}


def test_cell_pins_assignment_update_and_delete():
    ctx = Context()
    ctx.a = {}

    ctx.a.pins.b = {"c": 3}
    ctx.a["d"] = 4
    ctx.a.pins.update({"e": 5, "f": 6})

    assert ctx.a.value == {"b": {"c": 3}, "d": 4, "e": 5, "f": 6}

    del ctx.a["d"]
    del ctx.a.pins.e
    assert ctx.a.value == {"b": {"c": 3}, "f": 6}


def test_cell_subcell_depth_and_celltype_validation():
    ctx = Context()
    ctx.a = {}
    with pytest.raises(AttributeError):
        ctx.a.pins.b.c.set(3)

    cell = Cell(celltype="str")
    ctx.s = cell
    with pytest.raises(PathError):
        ctx.s.pins.x = "bad"


def test_incoming_edge_blocks_local_set_at_root():
    ctx = Context()
    ctx.a = 1
    ctx.b = ctx.a

    assert ctx.b.value == 1
    with pytest.raises(AuthorityError):
        ctx.b.set(2)


def test_graph_roundtrip_for_constants_and_edges():
    ctx = Context()
    ctx.a = 1
    ctx.b = ctx.a
    graph = ctx.get_graph(runtime=True)

    clone = Context()
    clone.set_graph(ctx.get_graph())
    assert clone.get_graph() == ctx.get_graph()
    assert clone.b.value == 1
    assert graph["nodes"][0]["runtime"]["state"] == "complete"


def test_subcontext_copy_drops_external_edges():
    ctx = Context()
    ctx.src = 1
    ctx.sub = Context()
    ctx.sub.a = 2
    ctx.sub.b = ctx.sub.a
    ctx.sub.c = ctx.src

    ctx.copy = ctx.sub
    assert ctx.copy.a.value == 2
    assert ctx.copy.b.value == 2
    assert ctx.copy.c.value is None
