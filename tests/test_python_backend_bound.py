"""Bound Python builders preserve BACKEND behavior across node views and graphs."""

import json

import pytest

from seamless_transformer import Transformer


def _python_builder(value=40, backend=None):
    tf = Transformer("python")
    if backend is not None:
        tf.metavars.BACKEND = backend
    tf.code = "result = value + 2"
    tf.celltypes.value = "int"
    tf.pins.value = value
    tf.celltypes.result = "int"
    tf.local = True
    return tf


def test_bound_python_backend_default_and_cli_execution(make_context):
    tf = _python_builder()
    ctx = make_context()
    ctx.tf = tf

    assert ctx.tf.metavars.BACKEND == "mem"
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 42

    ctx.tf.metavars.BACKEND = "cli"
    assert ctx.tf.metavars.BACKEND == "cli"
    ctx.compute(timeout=60)
    assert ctx.tf.result.value == 42


def test_bound_python_backend_views_share_value(make_context):
    ctx = make_context()
    ctx.tf = _python_builder()
    first = ctx.tf
    second = ctx.tf

    assert first is not second
    assert first.metavars.BACKEND == second.metavars.BACKEND == "mem"
    first.metavars.BACKEND = "cli"
    assert second.metavars.BACKEND == "cli"
    second.metavars.BACKEND = "mem"
    assert first.metavars.BACKEND == "mem"


def test_bound_python_backend_invalid_assignment_preserves_value(make_context):
    ctx = make_context()
    ctx.tf = _python_builder(backend="cli")

    for invalid in ("ryp", "juliacall", "Rscript", "python", "arbitrary", 7, None):
        with pytest.raises(ValueError, match="BACKEND"):
            ctx.tf.metavars.BACKEND = invalid
        assert ctx.tf.metavars.BACKEND == "cli"


def test_bound_python_backend_change_reuses_recorded_result(make_context, tmp_path):
    marker = tmp_path / "python-calls"
    tf = _python_builder(backend="cli")
    tf.code = 'open(marker, "a").write("executed\\n")\nresult = value + 2'
    tf.celltypes.marker = "str"
    tf.pins.marker = str(marker)
    ctx = make_context()
    ctx.tf = tf

    ctx.compute(timeout=60)
    assert ctx.tf.result.value == 42
    checksum = ctx.tf.build().construct()
    assert marker.read_text().splitlines() == ["executed"]

    ctx.tf.metavars.BACKEND = "mem"
    assert ctx.tf.build().construct() == checksum
    assert ctx.tf.build().meta["metavars"]["BACKEND"] == "mem"
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 42
    assert marker.read_text().splitlines() == ["executed"]


def test_bound_python_backend_graph_roundtrip(make_context):
    ctx = make_context()
    ctx.tf = _python_builder(backend="cli")
    ctx.compute(timeout=60)
    graph = json.loads(json.dumps(ctx.get_graph()))

    restored = make_context()
    restored.set_graph(graph)
    assert restored.tf.metavars.BACKEND == "cli"
    restored.compute(timeout=60)
    assert restored.tf.result.value == 42
