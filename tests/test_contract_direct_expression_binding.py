"""Binding a direct Expression that projects and converts in one operation.

expressions.md, *Fusion*: a path followed by a conversion is one Expression.
So a bound cell fed by `Expression(cs, path, X, Y)` is wired with the ordinary
links -- a path link and a conversion link -- which fuse back into that very
Expression.  The cell therefore needs no producer of its own kind, and
`get_graph()` saves it in the ordinary 0.5 format (workflow-context.md, *Graph
serialization*).
"""

import json

import pytest

from seamless import Cell, Expression


def _bind(ctx, code, celltype, *, path="code", outer=None):
    """Bind `b` to a direct Expression over `{"code": code}`; return (source, expression)."""
    source = Cell("plain")
    source.set({"code": code})
    expression = Expression(source, path=path, input_celltype="plain", celltype=celltype)
    final = celltype
    if outer is not None:
        expression = Expression(expression, input_celltype=celltype, celltype=outer)
        final = outer
    ctx.b = Cell(source=expression, celltype=final)
    ctx.compute(timeout=10)
    return source, expression


def _reload(make_context, graph):
    restored = make_context()
    restored.set_graph(json.loads(json.dumps(graph)))
    restored.compute(timeout=10)
    return restored


# "x = (" does not parse as python and "a: 1" reads differently as yaml text
# than as a JSON string: both tell the one Expression from an unfused pair.
CASES = [("x = 1", "python"), ("x = (", "python"), ("a: 1", "yaml"), ("value: [", "yaml")]


@pytest.mark.parametrize("code, celltype", CASES)
def test_bound_direct_expression_is_the_one_fused_expression(make_context, code, celltype):
    ctx = make_context()
    source, expression = _bind(ctx, code, celltype)
    assert ctx.b.state == "complete"
    assert ctx.b.exception is None
    assert ctx.b.checksum == expression.compute()
    assert ctx.b.build().identity_key == expression.identity_key


@pytest.mark.parametrize("code, celltype", CASES)
def test_bound_direct_expression_is_wired_with_ordinary_links(make_context, code, celltype):
    ctx = make_context()
    _bind(ctx, code, celltype)
    graph = ctx.get_graph()
    (entry,) = [node for node in graph["nodes"] if node["path"] == ["b"]]
    assert "expression" not in entry
    assert entry["value"] is None
    links = sorted(
        (anonymous["path"], anonymous["celltype"])
        for anonymous in graph["anonymous_nodes"].values()
    )
    assert links == [("", celltype), ("code", "plain")]
    (connection,) = graph["connections"]
    assert connection["target"] == ["b"]
    assert set(connection["source"]) == {"symbol"}


@pytest.mark.parametrize("code, celltype", CASES)
def test_bound_direct_expression_round_trips(make_context, code, celltype):
    ctx = make_context()
    source, expression = _bind(ctx, code, celltype)
    graph = ctx.get_graph()
    restored = _reload(make_context, graph)
    assert restored.b.state == "complete"
    assert restored.b.checksum == ctx.b.checksum == expression.compute()
    assert restored.b.build().identity_key == expression.identity_key
    assert restored.get_graph() == graph


def test_nested_direct_expression_round_trips(make_context):
    ctx = make_context()
    source, expression = _bind(ctx, "x = 1", "python", outer="text")
    assert ctx.b.state == "complete"
    assert ctx.b.checksum == expression.compute()
    graph = ctx.get_graph()
    links = sorted(
        (anonymous["path"], anonymous["celltype"])
        for anonymous in graph["anonymous_nodes"].values()
    )
    assert links == [("", "python"), ("", "text"), ("code", "plain")]
    restored = _reload(make_context, graph)
    assert restored.b.state == "complete"
    assert restored.b.checksum == ctx.b.checksum
    assert restored.get_graph() == graph


def test_failing_direct_expression_fails_the_same_after_a_reload(make_context):
    ctx = make_context()
    _bind(ctx, "x = 1", "python", path="missing")
    assert ctx.b.state == "failed"
    assert ctx.b.checksum is None
    graph = ctx.get_graph()
    restored = _reload(make_context, graph)
    assert restored.b.state == "failed"
    assert restored.b.exception == ctx.b.exception
    assert restored.get_graph() == graph
