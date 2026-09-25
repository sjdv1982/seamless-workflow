"""Bound-only Cell graph consequences of the 2026-09-21/22 rulings.

No Transformers are needed: conversion failures supply failed producers.
"""
import asyncio
import re
from threading import Event, Thread

import pytest
from seamless import Buffer, Cell, Expression
from seamless.checksum import expression as expression_module


def gap(reason):
    return pytest.mark.xfail(strict=False, reason="cells.md contract ahead of code: " + reason)


@gap("heterogeneous join members convert rather than embedding source values (Appendix F.2a)")
@pytest.mark.parametrize("join_type", ["plain", "mixed"])
def test_join_member_conversion_matches_root_connection(make_context, join_type):
    ctx = make_context()
    ctx.source = Cell("text")
    ctx.source.set("[1,2]")
    ctx.join = Cell(join_type)
    ctx.join.set({"kept": True})
    ctx.join["left"] = ctx.source
    ctx.root = Cell(join_type)
    ctx.root = ctx.source
    ctx.compute(timeout=10)
    assert ctx.join.value == {"kept": True, "left": ctx.root.value}
    assert ctx.root.value == [1, 2]
    assert ctx.join.checksum == Buffer({"kept": True, "left": [1, 2]}, join_type).get_checksum()


@gap("join conversion cannot share a link with a source projection")
def test_heterogeneous_join_refuses_projected_source(make_context):
    ctx = make_context()
    ctx.source = Cell("text")
    ctx.source.set("[1,2]")
    ctx.join = Cell("plain")
    with pytest.raises(TypeError, match="project"):
        ctx.join["left"] = ctx.source[0]


@gap("input_celltype must use the same path lookup as source")
def test_join_member_input_type_follows_its_source(make_context):
    ctx = make_context()
    ctx.source = Cell("text")
    ctx.source.set("[1,2]")
    ctx.join = Cell("plain")
    ctx.join.set({})
    ctx.join["left"] = ctx.source
    ctx.compute(timeout=10)
    assert ctx.join.source is None
    member = ctx.join["left"]
    assert member.celltype == "plain"
    assert member.input_celltype == member.source.celltype == "text"
    assert member.source.checksum == ctx.source.checksum


@gap("graph format 0.5 stores anonymous nodes separately (Appendix F.2a)")
def test_graph_version_and_named_nodes(make_context):
    ctx = make_context()
    ctx.named = Cell("plain")
    ctx.named.set(7)
    graph = ctx.get_graph()
    assert graph["__seamless_workflow__"] == "0.5"
    assert graph["anonymous_nodes"] == {}
    assert len(graph["nodes"]) == 1


@gap("anonymous chains, symbols, and fusion are not implemented")
def test_anonymous_nodes_roundtrip_with_stable_symbols(make_context):
    ctx = make_context()
    ctx.source = Cell("text")
    ctx.source.set("[10, 20, 30, 40]")
    ctx.result = ctx.source.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    assert ctx.result.value == 40
    graph = ctx.get_graph()
    assert graph["__seamless_workflow__"] == "0.5"
    symbols = set(graph["anonymous_nodes"])
    assert symbols
    assert all(re.fullmatch(r"[0-9a-f]{5}(?:-[1-9][0-9]*)?", symbol) for symbol in symbols)
    assert len(graph["nodes"]) == 2  # source and result, no anonymous duplicate
    for symbol in symbols:
        assert symbol not in dir(ctx)
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.result.value == 40
    assert restored.get_graph()["anonymous_nodes"] == graph["anonymous_nodes"]
    restored.source.set("[50, 60, 70, 80]")
    restored.compute(timeout=10)
    assert restored.result.value == 80
    assert set(restored.get_graph()["anonymous_nodes"]) == symbols
    del restored.result
    assert restored.get_graph()["anonymous_nodes"] == {}


@gap("named and anonymous chains must build the same maximal fused Expression")
def test_named_and_anonymous_intermediates_fuse_identically(make_context):
    ctx = make_context()
    ctx.source = Cell("plain")
    ctx.source.set({"a": {"b": 13}})
    ctx.named = ctx.source["a"]
    ctx.named_result = ctx.named["b"]
    ctx.anonymous_result = ctx.source["a"]["b"]
    ctx.compute(timeout=10)
    assert ctx.named.state == "complete"
    assert ctx.named.value == {"b": 13}
    assert ctx.named_result.value == ctx.anonymous_result.value == 13
    expected = Expression(ctx.source.checksum, input_celltype="plain", celltype="plain", path="a.b")
    assert ctx.named_result.build().identity_key == expected.identity_key
    assert ctx.anonymous_result.build().identity_key == expected.identity_key


@gap("miswired consumers and per-edge blocked-by-miswiring reasons")
def test_miswiring_blocks_dependents_and_recovers(make_context):
    ctx = make_context()
    ctx.source = Cell("text")
    ctx.source.set("[1,2]")
    ctx.child = ctx.source[1]
    ctx.dependent = ctx.child
    ctx.compute(timeout=10)
    ctx.source.celltype = "plain"
    ctx.compute(timeout=10)
    assert ctx.child.state == "miswired"
    assert ctx.child.checksum is None
    assert ctx.dependent.state == "blocked"
    # Only a cell with a one-level sub-path edge reports a dict (node-state-lifecycle.md).
    assert ctx.dependent.block_reason == "blocked-by-miswiring"
    ctx.source.celltype = "text"
    ctx.compute(timeout=10)
    assert ctx.child.value == ctx.dependent.value == "1"


@gap("Cell block_reason must enumerate every responsible input")
def test_join_reports_all_blocking_inputs(make_context):
    ctx = make_context()
    ctx.unwired = Cell("plain")
    ctx.failed = Cell("str")
    ctx.failed.set("not an integer")
    ctx.failed.celltype = "int"
    ctx.join = Cell("plain")
    ctx.join["left"] = ctx.unwired
    ctx.join["right"] = ctx.failed
    ctx.compute(timeout=10)
    assert ctx.join.state == "blocked"
    reasons = ctx.join.block_reason
    assert isinstance(reasons, dict)
    assert sorted(reasons.values()) == ["blocked-by-error", "blocked-by-unwired"]
    assert len(reasons) == 2
    assert ctx.join.exception is None


def test_bound_checksum_reads_do_not_join_active_evaluation(make_context, monkeypatch):
    """Gate core evaluation, not workflow internals; a getter must return early."""
    ctx = make_context()
    ctx.source = Cell("plain")
    ctx.source.set({"a": 109})
    ctx.compute(timeout=10)
    entered, release, returned = Event(), Event(), Event()
    original = expression_module._evaluate_expression_async

    async def delayed(*args, **kwargs):
        entered.set()
        if not await asyncio.to_thread(release.wait, 10):
            raise TimeoutError("test did not release evaluation")
        return await original(*args, **kwargs)

    monkeypatch.setattr(expression_module, "_evaluate_expression_async", delayed)
    ctx.projected = ctx.source["a"]
    result, errors = [], []

    def read():
        try:
            result.append(ctx.projected.checksum)
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()

    reader = Thread(target=read)
    try:
        assert entered.wait(5)
        reader.start()
        assert returned.wait(1), "bound checksum waited for active evaluation"
        assert not errors
        assert result == [None]
        assert ctx.projected.state == "waiting"
    finally:
        release.set()
        if reader.ident is not None:
            reader.join(5)
    ctx.compute(timeout=10)
    assert ctx.projected.value == 109


def test_projection_handle_checksum_pulls_over_the_parent_checksum(make_context):
    """cells.md §Reads, Anonymous and projection handles (2026-09-25 revision).

    Supersedes the F.2a item 5 reading that a bound getter must not evaluate an
    underived projection: a handle read builds and runs its Expression over the
    parent's checksum. Handle-local passive `.state` is pinned separately in
    test_contract_cells_handles.py.
    """
    ctx = make_context()
    ctx.root = Cell("plain")
    ctx.root.set({"a": 113})
    ctx.compute(timeout=10)
    projected = ctx.root["a"]
    assert projected.checksum == Buffer(113, "plain").get_checksum()
    assert projected.state == "complete"
    assert projected.compute() == Buffer(113, "plain").get_checksum()
    assert projected.value == 113


@gap("elision must suppress Expression construction for anonymous fusible intermediates")
@pytest.mark.parametrize("named", [False, True])
def test_only_anonymous_fusible_intermediates_are_elided(make_context, monkeypatch, named):
    ctx = make_context()
    ctx.root = Cell("plain")
    ctx.root.set({"a": {"b": 167}})
    ctx.compute(timeout=10)
    built_paths = []
    initialize = Expression.__post_init__

    def record(expression):
        initialize(expression)
        built_paths.append(expression.path)

    monkeypatch.setattr(Expression, "__post_init__", record)
    if named:
        ctx.mid = ctx.root["a"]
        ctx.result = ctx.mid["b"]
    else:
        ctx.result = ctx.root["a"]["b"]
    ctx.compute(timeout=10)
    assert ctx.result.value == 167
    assert "a.b" in built_paths
    assert ("a" in built_paths) is named
    if named:
        assert ctx.mid.state == "complete"
        assert ctx.mid.value == {"b": 167}


@gap("0.5 loaders must preserve stored collision suffixes rather than mint symbols again")
def test_stored_collision_suffixes_survive_graph_load(make_context):
    ctx = make_context()
    ctx.first = Cell("text")
    ctx.first.set("[1,2]")
    ctx.second = Cell("text")
    ctx.second.set("[3,4]")
    ctx.a = ctx.first.as_celltype("plain")[0]
    ctx.b = ctx.second.as_celltype("plain")[0]
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    symbols = list(graph["anonymous_nodes"])
    assert len(symbols) == 2
    # Exercise the durable collision representation independently of the hash
    # function: loading must not recompute the five-hex symbol or its suffix.
    replacement = dict(zip(symbols, ["abcde", "abcde-1"]))

    def rename(value):
        if isinstance(value, str):
            return replacement.get(value, value)
        if isinstance(value, dict):
            return {replacement.get(key, key): rename(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [rename(item) for item in value]
        return value

    graph = rename(graph)
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.a.value == 1
    assert restored.b.value == 3
    assert set(restored.get_graph()["anonymous_nodes"]) == {"abcde", "abcde-1"}
