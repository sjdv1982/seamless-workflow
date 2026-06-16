from __future__ import annotations

from seamless import Cell
from seamless_workflow import Context


def add(x, y=10):
    return x + y


def test_runtime_records_current_and_superseded_runs_with_prune():
    ctx = Context()
    ctx.a = 1
    first = ctx.get_graph(runtime=True)["nodes"][0]["runtime"]["run"]["current"]

    ctx.a = 2
    graph = ctx.get_graph(runtime=True)
    runtime = graph["nodes"][0]["runtime"]["run"]

    assert runtime["current"]["result"] != first["result"]
    assert len(runtime["superseded"]) == 1
    assert runtime["superseded"][0]["phase"] == "superseded"
    assert ctx.prune() == {"cancelled": 1}
    assert ctx.get_graph(runtime=True)["nodes"][0]["runtime"]["run"]["superseded"] == []


def test_superseded_run_cap_is_three_per_node():
    ctx = Context()
    for value in range(6):
        ctx.a = value

    superseded = ctx.get_graph(runtime=True)["nodes"][0]["runtime"]["run"]["superseded"]
    assert len(superseded) == 3
    assert all(record["phase"] == "superseded" for record in superseded)


def test_node_level_prune_scopes_to_downstream_cone():
    ctx = Context()
    ctx.a = 1
    ctx.b = ctx.a
    ctx.c = 10
    ctx.a = 2
    ctx.c = 11

    assert ctx.a.prune() == {"cancelled": 2}
    runtime = {tuple(node["path"]): node["runtime"]["run"] for node in ctx.get_graph(runtime=True)["nodes"]}
    assert runtime[("a",)]["superseded"] == []
    assert runtime[("b",)]["superseded"] == []
    assert len(runtime[("c",)]["superseded"]) == 1


def test_connected_optional_pin_participates_but_absent_optional_is_skipped():
    ctx = Context()
    ctx.add = add
    ctx.add.optional_pins = {"y"}
    ctx.add.pins.x = 5

    assert ctx.add.result.value == 15
    ctx.add.pins.y = 3
    assert ctx.add.result.value == 8


def test_standalone_cell_pins_build_and_run_dictionary():
    cell = Cell(celltype="mixed")
    cell.pins.x = 1
    cell.pins["y"] = 2

    assert cell.run() == {"x": 1, "y": 2}
