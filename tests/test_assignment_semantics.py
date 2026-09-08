from __future__ import annotations

import pytest

from seamless import Cell
from seamless_workflow import Context


def echo(x):
    # The worker rejects an empty result; wrapping distinguishes a concrete
    # null input from an unwired pin without relying on empty-result semantics.
    return {"x": x}


def test_assigning_same_value_does_not_supersede_current_run():
    ctx = Context()
    ctx.a = {"x": 1}
    first = ctx.get_graph(runtime=True)["nodes"][0]["runtime"]["run"]["current"]

    ctx.a = {"x": 1}
    second_runtime = ctx.get_graph(runtime=True)["nodes"][0]["runtime"]["run"]

    assert second_runtime["current"]["generation"] == first["generation"]
    assert second_runtime["superseded"] == []


def test_transformer_pin_none_is_a_value_and_del_is_deletion():
    ctx = Context()
    ctx.echo = echo
    ctx.echo.pins.x = 1
    ctx.compute(timeout=10)
    assert ctx.echo.result.value == {"x": 1}

    ctx.echo.pins.x = None
    ctx.compute(timeout=10)
    assert ctx._graph.nodes[("echo",)].state == "complete"
    assert ctx.echo.result.value == {"x": None}

    ctx.echo.pins.x = 2
    ctx.compute(timeout=10)
    assert ctx.echo.result.value == {"x": 2}
    del ctx.echo.pins.x
    assert ctx._graph.nodes[("echo",)].state == "unwired"


def test_standalone_cell_captures_complete_bound_source_checksum():
    ctx = Context()
    ctx.a = {"v": 1}

    target = Cell()
    target.set(ctx.a)
    ctx.a = {"v": 2}

    assert target.run() == {"v": 1}


def test_standalone_cell_capture_rejects_unwired_source():
    ctx = Context()
    ctx.a = Cell()

    target = Cell()
    with pytest.raises(ValueError):
        target.set(ctx.a)


def test_source_replacement_validates_before_detaching_previous_edge():
    from seamless_workflow import DependencyError
    ctx = Context()
    ctx.a = 1
    ctx.b = 2
    ctx.target = ctx.a
    ctx.downstream = ctx.target
    before = ctx.get_graph()
    with pytest.raises(DependencyError):
        ctx.target = ctx.downstream
    assert ctx.get_graph() == before
    assert ctx.target.value == 1
    ctx.target = ctx.b
    assert ctx.target.value == 2
    assert ctx.downstream.value == 2
    ctx.close()
