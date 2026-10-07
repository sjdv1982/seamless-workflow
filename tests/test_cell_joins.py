"""Observable contracts for Cells assembled from sub-path producers."""

from __future__ import annotations

from threading import Event

import pytest

from seamless import Cell


def test_join_assembles_root_and_subpath_values_at_its_declared_celltype(
    make_context,
):
    ctx = make_context()
    ctx.left = 12
    ctx.right = {"name": "replacement"}
    ctx.join = Cell("plain")
    ctx.join.set({"kept": True, "left": 0, "right": None})
    ctx.join.left = ctx.left
    ctx.join.right = ctx.right

    ctx.compute(timeout=10)

    assert ctx.join.celltype == "plain"
    assert ctx.join.value == {
        "kept": True,
        "left": 12,
        "right": {"name": "replacement"},
    }
    assert ctx.right.value == {"name": "replacement"}


def test_join_reacts_to_edits_and_reuses_checksum_after_revert(make_context):
    ctx = make_context()
    ctx.source = "first"
    ctx.join = Cell("plain")
    ctx.join["value"] = ctx.source
    ctx.compute(timeout=10)
    first_checksum = ctx.join.checksum

    ctx.source = "second"
    ctx.compute(timeout=10)
    second_checksum = ctx.join.checksum
    assert ctx.join.value == {"value": "second"}
    assert second_checksum != first_checksum

    ctx.source = "first"
    ctx.compute(timeout=10)
    assert ctx.join.value == {"value": "first"}
    assert ctx.join.checksum == first_checksum


def test_join_is_blocked_by_error_when_an_upstream_fails(make_context):
    ctx = make_context()
    ctx.broken = Cell("str")
    ctx.broken.set("not an integer")
    ctx.broken.celltype = "int"
    ctx.join = Cell("plain")
    ctx.join["value"] = ctx.broken

    ctx.compute(timeout=10)

    assert ctx.broken.state == "failed"
    assert ctx.join.state == "blocked"
    assert ctx.join.block_reason == {"value": "blocked-by-error"}
    assert ctx.join.exception is None


def test_join_is_blocked_by_unwired_when_an_upstream_is_unwired(make_context):
    ctx = make_context()
    ctx.source = Cell("plain")
    ctx.join = Cell("plain")
    ctx.join["value"] = ctx.source

    ctx.compute(timeout=10)

    assert ctx.source.state == "unwired"
    assert ctx.join.state == "blocked"
    assert ctx.join.block_reason == {"value": "blocked-by-unwired"}
    assert ctx.join.exception is None


def test_join_waits_for_local_sidework_without_entering_computing(
    make_context, monkeypatch
):
    import seamless_workflow.context as context_module

    entered = Event()
    release = Event()
    original_evaluate_cell = context_module.evaluate_cell

    def delayed_evaluate_cell(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original_evaluate_cell(*args, **kwargs)

    monkeypatch.setattr(context_module, "evaluate_cell", delayed_evaluate_cell)
    ctx = make_context()
    ctx.source = 42
    ctx.join = Cell("plain")
    ctx.join["value"] = ctx.source

    try:
        assert entered.wait(timeout=10)
        assert ctx.join.state == "waiting"
    finally:
        release.set()

    ctx.compute(timeout=10)
    assert ctx.join.state == "complete"
    assert ctx.join.value == {"value": 42}


def test_join_build_is_a_snapshot_expression_not_a_join_recipe(make_context):
    ctx = make_context()
    ctx.source = "before"
    ctx.join = Cell("plain")
    ctx.join["value"] = ctx.source
    ctx.compute(timeout=10)

    joined_checksum = ctx.join.checksum
    snapshot = ctx.join.build()
    assert snapshot.input_checksum == joined_checksum
    assert snapshot.path == ""
    assert snapshot.run() == {"value": "before"}

    ctx.source = "after"
    ctx.compute(timeout=10)
    assert ctx.join.value == {"value": "after"}
    assert snapshot.run() == {"value": "before"}


@pytest.mark.parametrize("deep_type,member_type,value", [
    ("deepcell", "mixed", {"a": [1, 2]}),
    ("deepfolder", "bytes", b"contents"),
    ("folder", "bytes", b"contents"),
])
def test_deep_join_inserts_member_checksum_and_reacts(
    make_context, deep_type, member_type, value
):
    ctx = make_context()
    ctx.d = Cell(deep_type)
    ctx.x = Cell(member_type)
    ctx.x.set(value)
    ctx.d["k"] = ctx.x
    ctx.compute(timeout=10)
    assert ctx.d.state == "complete"
    assert ctx.d.value == {"k": ctx.x.checksum}
    assert ctx.d["k"].value == value
    original = ctx.d.checksum
    ctx.x.set(None if member_type == "mixed" else b"changed")
    ctx.compute(timeout=10)
    assert ctx.d.checksum != original
    ctx.x.set(value)
    ctx.compute(timeout=10)
    assert ctx.d.checksum == original
    ctx.x.celltype = "plain" if member_type == "mixed" else "text"
    ctx.compute(timeout=10)
    assert ctx.d.state == "miswired"
    ctx.d.celltype = "plain"
    ctx.compute(timeout=10)
    assert ctx.d.state == "miswired"
    copy = make_context()
    copy.set_graph(ctx.get_graph())
    copy.compute(timeout=10)
    assert copy.d.state == "miswired"
    assert ctx.d.exception is None
    ctx.x.celltype = member_type
    ctx.d.celltype = deep_type
    ctx.compute(timeout=10)
    assert ctx.d.state == "complete"
    ctx.d.celltype = "deepfolder" if deep_type == "deepcell" else "deepcell"
    ctx.compute(timeout=10)
    assert ctx.d.state == "miswired"


@pytest.mark.parametrize("deep_type", ["deepcell", "deepfolder", "folder"])
@pytest.mark.parametrize("source_type", ["plain", "mixed", "bytes", "text", "checksum", "deepcell", "deepfolder", "folder"])
def test_deep_join_refuses_other_member_celltypes(make_context, deep_type, source_type):
    member_type = "mixed" if deep_type == "deepcell" else "bytes"
    if source_type == member_type:
        return
    ctx = make_context()
    ctx.d = Cell(deep_type)
    ctx.x = Cell(source_type)
    before = ctx.get_graph()
    with pytest.raises(TypeError):
        ctx.d["k"] = ctx.x
    assert ctx.get_graph() == before


@pytest.mark.parametrize("deep_type,member_type,value", [
    ("deepcell", "mixed", {"a": 1}),
    ("deepfolder", "bytes", b"contents"),
    ("folder", "bytes", b"contents"),
])
def test_deep_join_literal_root_projection_and_failure(
    make_context, deep_type, member_type, value
):
    ctx = make_context()
    ctx.base = Cell(deep_type)
    ctx.base["kept"].set(value)
    ctx.d = Cell(deep_type)
    ctx.d.set_checksum(ctx.base.checksum)
    ctx.x = Cell(member_type)
    ctx.x.set(value)
    ctx.d["k"] = ctx.x
    ctx.other = Cell(deep_type)
    ctx.other["projected"] = ctx.d["k"]
    ctx.compute(timeout=10)
    assert ctx.d.value == {"kept": ctx.x.checksum, "k": ctx.x.checksum}
    assert ctx.other["projected"].value == value
    with pytest.raises(Exception):
        ctx.d = ctx.base
    ctx.root = Cell(deep_type)
    ctx.root = ctx.base
    with pytest.raises(Exception):
        ctx.root["k"] = ctx.x
    ctx.failed = Cell(member_type, checksum=ctx.x.checksum,
                      validator=ctx.x.checksum, validator_language="python")
    ctx.failure_join = Cell(deep_type)
    ctx.failure_join["k"] = ctx.failed
    ctx.compute(timeout=10)
    assert ctx.failed.state == "failed"
    assert ctx.failure_join.state == "blocked"
    assert ctx.failure_join.block_reason == {"k": "blocked-by-error"}


@pytest.mark.parametrize("deep_type,member_type,value", [
    ("deepcell", "mixed", {"a": 1}),
    ("deepfolder", "bytes", b"contents"),
    ("folder", "bytes", b"contents"),
])
def test_deep_join_graph_reload_and_unwired_member(
    make_context, deep_type, member_type, value
):
    ctx = make_context()
    ctx.d = Cell(deep_type)
    ctx.x = Cell(member_type)
    ctx.d["k"] = ctx.x
    ctx.compute(timeout=10)
    assert ctx.d.state == "blocked"
    assert ctx.d.block_reason == {"k": "blocked-by-unwired"}
    with pytest.raises(ValueError):
        ctx.d[0] = ctx.x
    ctx.x.set(value)
    ctx.compute(timeout=10)
    copy = make_context()
    copy.set_graph(ctx.get_graph())
    copy.compute(timeout=10)
    assert copy.d.value == {"k": copy.x.checksum}
    ctx.x.celltype = "plain" if member_type == "mixed" else "text"
    copy.set_graph(ctx.get_graph())
    copy.compute(timeout=10)
    assert copy.d.state == "miswired"


def test_deep_join_accepts_explicit_member_conversion(make_context):
    ctx = make_context()
    ctx.d = Cell("deepcell")
    ctx.x = Cell("plain")
    ctx.x.set({"a": 1})
    ctx.d["k"] = ctx.x.as_celltype("mixed")
    ctx.compute(timeout=10)
    assert ctx.d.state == "complete"
    assert ctx.d["k"].value == {"a": 1}
    copy = make_context()
    copy.set_graph(ctx.get_graph())
    copy.compute(timeout=10)
    assert copy.d["k"].value == {"a": 1}
