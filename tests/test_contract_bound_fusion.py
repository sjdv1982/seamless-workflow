"""Fusion and elision over anonymous intermediates in bound mode.

Contract: contracts/expressions.md, *Fusion* and *Identity*; contracts/cells.md,
*Anonymous cells, symbols and elision*.
"""
import pytest

from seamless import Buffer, Cell, Expression
from seamless_workflow import Context
from seamless_workflow.graph import anonymous_links, fusible_runs


def double(x):
    return 2 * x


@pytest.mark.parametrize("source, steps, path, expected", [
    ("mixed", ((0, "plain"),), (3,), [("plain", (3,), "plain", 1)]),
    ("text", ((0, "plain"),), (3,), [("text", (), "plain", 0), ("plain", (3,), "plain", 1)]),
    ("plain", ((1, "int"),), (3,), [("plain", (3,), "int", 1)]),
    ("mixed", ((0, "plain"), (1, "int")), (3,), [("plain", (3,), "int", 2)]),
])
def test_fusible_runs_table_from_anonymous_links(source, steps, path, expected):
    links = anonymous_links(source, path, steps)
    assert fusible_runs(source, links) == expected


@pytest.mark.parametrize("source, links, expected", [
    ("plain", [("plain", ("a",)), ("int", ()), ("float", ())],
     [("plain", ("a",), "int", 1), ("int", (), "float", 2)]),
    ("text", [("plain", ()), ("mixed", ())],
     [("text", (), "plain", 0), ("plain", (), "mixed", 1)]),
    ("deepcell", [("mixed", ("k",)), ("mixed", ("a",))],
     [("deepcell", ("k",), "mixed", 0), ("mixed", ("a",), "mixed", 1)]),
])
def test_fusible_runs_table_literal(source, links, expected):
    assert fusible_runs(source, links) == expected


def _record(monkeypatch):
    built = []
    initialize = Expression.__post_init__

    def record(expression):
        initialize(expression)
        built.append((expression.input_celltype, expression.celltype, expression.path))

    monkeypatch.setattr(Expression, "__post_init__", record)
    return built


def _source(ctx, celltype):
    ctx.b = Cell(celltype)
    ctx.b.set("[10, 20, 30, 40]" if celltype == "text" else [10, 20, 30, 40])
    ctx.compute(timeout=10)


def test_path_then_conversion_is_one_expression(make_context, monkeypatch):
    ctx = make_context()
    _source(ctx, "plain")
    built = _record(monkeypatch)
    ctx.a = ctx.b[3].as_celltype("int")
    ctx.compute(timeout=10)
    assert ctx.a.value == 40
    assert ("plain", "int", "[3]") in built
    assert ("plain", "plain", "[3]") not in built
    assert ("plain", "int", "") not in built


def test_preserving_conversion_path_conversion_is_one_expression(make_context, monkeypatch):
    ctx = make_context()
    _source(ctx, "mixed")
    built = _record(monkeypatch)
    ctx.a = ctx.b.as_celltype("plain")[3].as_celltype("int")
    ctx.compute(timeout=10)
    assert ctx.a.value == 40
    assert ("plain", "int", "[3]") in built
    assert ("mixed", "plain", "") not in built
    assert ("plain", "plain", "[3]") not in built


def test_new_buffer_conversion_is_a_barrier(make_context, monkeypatch):
    ctx = make_context()
    _source(ctx, "text")
    built = _record(monkeypatch)
    ctx.a = ctx.b.as_celltype("plain")[3].as_celltype("int")
    ctx.compute(timeout=10)
    assert ctx.a.value == 40
    assert ("text", "plain", "") in built
    assert ("plain", "int", "[3]") in built
    assert ("plain", "plain", "[3]") not in built


def _anonymous_roles(ctx):
    return [role for _checksum, role in ctx._refheld_checksums()
            if role.startswith("anonymous:")]


def test_elided_anonymous_cell_holds_no_checksum(make_context):
    ctx = make_context()
    _source(ctx, "mixed")
    ctx.a = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    assert ctx.a.value == 40
    assert _anonymous_roles(ctx) == []

    other = make_context()
    _source(other, "text")
    other.a = other.b.as_celltype("plain")[3]
    other.compute(timeout=10)
    assert other.a.value == 40
    roles = _anonymous_roles(other)
    assert len(roles) == 1
    expected = Expression(other.b.checksum, input_celltype="text", celltype="plain").compute()
    held = [checksum for checksum, role in other._refheld_checksums()
            if role == roles[0]]
    assert held == [expected]


def test_fused_recipe_feeds_a_transformer_pin(make_context, monkeypatch):
    ctx = make_context()
    _source(ctx, "mixed")
    built = _record(monkeypatch)
    ctx.tf = double
    ctx.tf.celltypes.x = "plain"
    ctx.tf.pins.x = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 80
    assert ("mixed", "plain", "") not in built


def test_get_graph_still_saves_both_links_of_an_elided_chain(make_context):
    ctx = make_context()
    _source(ctx, "mixed")
    ctx.a = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    assert len(graph["anonymous_nodes"]) == 2
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.a.value == 40
