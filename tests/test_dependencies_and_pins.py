from __future__ import annotations

import pytest

from seamless import Cell
from seamless_workflow import Context
from seamless_workflow.errors import DependencyError


def pick(d):
    return d["left"] + d["right"]


def identity(scratch):
    return scratch


def test_bound_edges_into_cell_pins_and_transformer_pins():
    ctx = Context()
    ctx.src = 4
    ctx.box = {}
    ctx.box.pins.value = ctx.src

    assert ctx.box.value == {"value": 4}

    ctx.pick = pick
    ctx.pick.pins.d.left.set(ctx.src)
    ctx.pick.pins.d.right = 6
    assert ctx.pick.result.value == 10


def test_recursive_cell_builder_binding_uses_pseudo_anonymous_node():
    upstream = Cell(12)
    downstream = Cell(upstream)

    ctx = Context()
    ctx.result = downstream

    graph = ctx.get_graph()
    paths = {tuple(node["path"]) for node in graph["nodes"]}
    assert ("result",) in paths
    assert ("cell1",) in paths
    assert ctx.result.value == 12
    assert downstream.value == 12


def test_cross_top_level_dependency_rejected():
    ctx1 = Context()
    ctx2 = Context()
    ctx1.a = 1

    with pytest.raises(DependencyError):
        ctx2.b = ctx1.a


def test_transformer_pin_named_scratch_uses_item_syntax_when_colliding():
    ctx = Context()
    ctx.tf = identity
    ctx.tf["scratch"] = "pin-value"

    assert ctx.tf.scratch is False
    ctx.tf.scratch = True
    assert ctx.tf.scratch is True
    assert ctx.tf.result.value == "pin-value"


def test_transformer_pin_overlays_roundtrip():
    ctx = Context()
    ctx.pick = pick
    ctx.pick.pins.d.left = 8
    ctx.pick.pins.d.right = 9

    clone = Context()
    clone.set_graph(ctx.get_graph())
    assert clone.get_graph() == ctx.get_graph()
