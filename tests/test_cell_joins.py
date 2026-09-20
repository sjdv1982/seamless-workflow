"""Observable contracts for Cells assembled from sub-path producers."""

from __future__ import annotations

from threading import Event

from seamless import Cell


def fail(value):
    raise RuntimeError(f"cannot use {value}")


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
    ctx.join.value = ctx.source
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
    ctx.broken = fail
    ctx.broken.pins.value = 1
    ctx.join = Cell("plain")
    ctx.join.value = ctx.broken

    ctx.compute(timeout=10)

    assert ctx.broken.state == "failed"
    assert ctx.join.state == "blocked"
    assert ctx.join.block_reason == "blocked-by-error"
    assert ctx.join.exception is None


def test_join_is_blocked_by_unwired_when_an_upstream_is_unwired(make_context):
    ctx = make_context()
    ctx.source = Cell("plain")
    ctx.join = Cell("plain")
    ctx.join.value = ctx.source

    ctx.compute(timeout=10)

    assert ctx.source.state == "unwired"
    assert ctx.join.state == "blocked"
    assert ctx.join.block_reason == "blocked-by-unwired"
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
    ctx.join.value = ctx.source

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
    ctx.join.value = ctx.source
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
