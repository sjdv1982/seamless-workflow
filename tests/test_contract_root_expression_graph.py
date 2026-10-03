"""A bound cell's retained root Expression survives get_graph / set_graph.

contracts/workflow-context.md, *Graph serialization*: a cell whose producer is
one direct Expression (a link that projects and converts in one operation, or
a chain that is not a plain checksum projection) is saved as an optional
``"expression"`` recipe on its cell entry; the format stays "0.5".  The recipe
is the Expression's definition (contracts/expressions.md, *The definition*):
input, path, input celltype, celltype, and the validator when there is one.
"""

import copy
import json

import pytest

from seamless import Cell, Expression
from seamless_workflow.errors import PathError


def _bind(ctx, *, path="code", celltype="python", nested=False):
    source = Cell("plain")
    source.set({"code": "x = 1"})
    expression = Expression(
        source, path=path, input_celltype="plain", celltype=celltype,
    )
    if nested:
        expression = Expression(expression, input_celltype=celltype, celltype="text")
        celltype = "text"
    ctx.b = Cell(source=expression, celltype=celltype)
    ctx.compute(timeout=10)
    # The source Cell is returned so that its buffer stays alive.
    return source, expression


def _entry(graph, name="b"):
    return next(e for e in graph["nodes"] if e["path"] == [name])


def _restore(make_context, graph):
    ctx2 = make_context()
    ctx2.set_graph(graph)
    ctx2.compute(timeout=10)
    return ctx2


def _projection(source):
    return {
        "input": {"checksum": source.checksum.hex()},
        "path": "code",
        "input_celltype": "plain",
        "celltype": "python",
    }


def _recipe(source):
    # The bound Cell wraps the Expression it was given in an identity
    # Expression; the graph records exactly what the Cell retains.
    return {
        "input": {"expression": _projection(source)},
        "path": "",
        "input_celltype": "python",
        "celltype": "python",
    }


def _innermost(recipe):
    while "expression" in recipe["input"]:
        recipe = recipe["input"]["expression"]
    return recipe


def test_combined_root_expression_round_trips(make_context):
    ctx = make_context()
    source, expression = _bind(ctx)
    graph = ctx.get_graph()
    entry = _entry(graph)
    assert graph["__seamless_workflow__"] == "0.5"
    assert entry["value"] is None
    assert entry["expression"] == _recipe(source)

    ctx2 = _restore(make_context, graph)
    assert ctx2.b.state == "complete"
    assert ctx2.b.checksum == ctx.b.checksum == expression.compute()
    assert ctx2.b.build().identity_key == ctx.b.build().identity_key
    assert ctx2.get_graph() == graph


def test_nested_root_expression_round_trips(make_context):
    ctx = make_context()
    source, _ = _bind(ctx, nested=True)
    graph = ctx.get_graph()
    recipe = _entry(graph)["expression"]
    assert recipe == {
        "input": {"expression": {
            "input": {"expression": _projection(source)},
            "path": "",
            "input_celltype": "python",
            "celltype": "text",
        }},
        "path": "",
        "input_celltype": "text",
        "celltype": "text",
    }

    ctx2 = _restore(make_context, graph)
    assert ctx2.b.state == "complete"
    assert ctx2.b.checksum == ctx.b.checksum
    assert ctx2.b.build().identity_key == ctx.b.build().identity_key
    assert ctx2.get_graph() == graph


def test_json_round_trip(make_context):
    ctx = make_context()
    source, _ = _bind(ctx, nested=True)
    graph = ctx.get_graph()
    loaded = json.loads(json.dumps(graph))
    assert loaded == graph

    ctx2 = _restore(make_context, loaded)
    assert ctx2.b.state == "complete"
    assert ctx2.b.checksum == ctx.b.checksum
    assert ctx2.get_graph() == graph


def test_runtime_graph_also_carries_the_recipe(make_context):
    ctx = make_context()
    source, _ = _bind(ctx)
    entry = _entry(ctx.get_graph(runtime=True))
    assert entry["expression"] == _recipe(source)
    assert entry["value"] is None


def test_literal_and_link_cells_carry_no_expression_key(make_context):
    ctx = make_context()
    ctx.a = Cell("plain")
    ctx.a.set({"code": "x = 1"})
    ctx.b = Cell("plain", source=ctx.a)
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    for name in ("a", "b"):
        assert "expression" not in _entry(graph, name)
    assert _entry(graph, "a")["value"] is not None


def _refusal_cases():
    def with_value(graph):
        entry = _entry(graph)
        entry["value"] = {"checksum": "a" * 64, "celltype": "python"}

    def old_version(graph):
        graph["__seamless_workflow__"] = "0.4"

    def missing_key(graph):
        del _entry(graph)["expression"]["path"]

    def unknown_key(graph):
        _entry(graph)["expression"]["extra"] = 1

    def both_inputs(graph):
        recipe = _innermost(_entry(graph)["expression"])
        recipe["input"] = {
            "checksum": recipe["input"]["checksum"],
            "expression": copy.deepcopy(recipe),
        }

    def non_hex(graph):
        _innermost(_entry(graph)["expression"])["input"] = {"checksum": "z" * 64}

    def unknown_celltype(graph):
        _entry(graph)["expression"]["celltype"] = "no_such_celltype"

    def not_a_dict(graph):
        _entry(graph)["expression"] = ["input"]

    def validator_without_language(graph):
        _entry(graph)["expression"]["validator"] = "a" * 64

    def connection_into_cell(graph):
        graph["nodes"].append({
            "type": "cell", "path": ["c"], "celltype": "plain", "validator": None,
            "validator_language": None, "scratch": False,
            "value": {"checksum": "a" * 64, "celltype": "plain"},
        })
        graph["connections"].append(
            {"type": "connection", "target": ["b"], "source": {"node": ["c"]}}
        )

    return [
        with_value, old_version, missing_key, unknown_key, both_inputs,
        non_hex, unknown_celltype, not_a_dict, validator_without_language,
        connection_into_cell,
    ]


@pytest.mark.parametrize("corrupt", _refusal_cases(), ids=lambda f: f.__name__)
def test_loader_refuses_malformed_recipes(make_context, corrupt):
    ctx = make_context()
    _bind(ctx)
    graph = ctx.get_graph()
    corrupt(graph)
    with pytest.raises(PathError):
        make_context().set_graph(graph)


def test_restored_root_expression_failure_is_the_same(make_context):
    ctx = make_context()
    source, _ = _bind(ctx, path="missing")
    assert ctx.b.state == "failed"
    graph = ctx.get_graph()
    assert _innermost(_entry(graph)["expression"])["path"] == "missing"

    ctx2 = _restore(make_context, graph)
    assert ctx2.b.state == "failed"
    assert ctx2.b.checksum is None
    assert ctx2.b.exception == ctx.b.exception
    assert ctx2.get_graph() == graph
