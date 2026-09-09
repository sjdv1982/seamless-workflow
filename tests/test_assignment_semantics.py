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


@pytest.mark.parametrize("route", ["attribute", "item", "set"])
@pytest.mark.parametrize("celltype, value", [("mixed", {"v": 1}), ("plain", [1, 2]), ("int", 42)])
def test_setting_cell_none_clears_checksum_and_unwires_downstream(make_context, route, celltype, value):
    ctx = make_context()
    ctx.a = Cell(celltype=celltype)
    ctx.a.set(value)
    ctx.out = ctx.a
    ctx.echo = echo
    ctx.echo.pins.x = ctx.a
    ctx.compute(timeout=10)
    checksum = ctx.a.checksum
    assert checksum is not None
    assert ctx.echo.result.value == {"x": value}

    def clear():
        if route == "attribute":
            ctx.a = None
        elif route == "item":
            ctx["a"] = None
        else:
            ctx.a.set(None)

    clear()
    clear()  # Clearing an already unwired cell is idempotent.
    ctx.compute(timeout=10)
    assert ctx.a.checksum is None
    assert ctx.a.state == "unwired"
    assert ctx.a.exception is None
    assert ctx.out.checksum is None
    assert ctx.out.state == "blocked"
    assert ctx.out.block_reason == "blocked-by-unwired"
    assert ctx.echo.state == "blocked"
    assert ctx.echo.block_reason == ["x"]

    ctx.a.set(value)
    ctx.compute(timeout=10)
    assert ctx.a.state == "complete"
    assert ctx.a.checksum == checksum
    assert ctx.echo.result.value == {"x": value}


def test_new_cell_assigned_none_is_unwired(make_context):
    ctx = make_context()
    ctx.a = None
    assert isinstance(ctx.a, Cell)
    assert ctx.a.checksum is None
    assert ctx.a.state == "unwired"


def test_assigning_none_detaches_old_upstream_connection(make_context):
    ctx = make_context()
    ctx.source = 1
    ctx.target = ctx.source
    ctx.target = None
    assert ctx.target.checksum is None
    assert ctx.target.state == "unwired"
    ctx.source = 2
    assert ctx.target.checksum is None
    assert ctx.target.state == "unwired"


def test_structural_null_values_remain_values(make_context):
    ctx = make_context()
    ctx.a = {"nested": None}
    assert ctx.a.checksum is not None
    assert ctx.a.state == "complete"
    ctx.a.nested = None
    assert ctx.a.value == {"nested": None}
    assert ctx.a.state == "complete"


def test_clearing_cell_releases_its_checksum_references(make_context):
    from seamless.caching.buffer_cache import get_buffer_cache

    ctx = make_context()
    ctx.a = {"unique": "cell-none-reference-test"}
    checksum = ctx.a.checksum
    assert get_buffer_cache().reference_snapshot()[checksum][0] == 2
    ctx.a.set(None)
    assert not any(role in {"cell:a:literal", "node:a:current"}
                   for _, role in ctx._refheld_checksums())
    # Superseded runtime results may retain their own leases until close.
    ctx.close()
    assert get_buffer_cache().reference_snapshot().get(checksum, (0,))[0] == 0
