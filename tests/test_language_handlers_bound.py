"""Bound handler builders retain declared metavars outside execution identity."""
import uuid

import pytest

from seamless_transformer import Transformer
from seamless_transformer.languages import register_language_handler


def bound_handler(pins, metavars):
    from pathlib import Path
    marker = Path(pins["marker"])
    with marker.open("a") as stream:
        stream.write("executed\n")
    return pins["value"] + metavars["OFFSET"]


def test_bound_handler_metavars_defaults_persistence_and_cache_hit(make_context, tmp_path):
    name = "bound-toy-" + uuid.uuid4().hex
    register_language_handler(name, bound_handler,
                              metavars={"BACKEND": "cli", "OFFSET": 3})
    tf = Transformer(name)
    tf.code = "foreign code"
    tf.celltypes.result = "plain"
    tf.pins.value = 12
    tf.celltypes.marker = "str"
    marker = tmp_path / "calls"
    tf.pins.marker = str(marker)
    tf.local = True
    ctx = make_context()
    ctx.tf = tf
    assert ctx.tf.metavars.BACKEND == "cli"
    assert ctx.tf.metavars.OFFSET == 3
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 15
    checksum = ctx.tf.build().construct()
    graph_before = ctx.get_graph()
    ctx.tf.metavars.BACKEND = "memory"
    assert ctx.tf.metavars.BACKEND == "memory"
    assert ctx.tf.metavars.OFFSET == 3
    assert ctx.get_graph() != graph_before
    with pytest.raises((AttributeError, KeyError, ValueError)):
        ctx.tf.metavars.UNDECLARED = 1
    snapshot = ctx.tf.build()
    assert snapshot.construct() == checksum
    assert snapshot.meta["metavars"] == {"BACKEND": "memory", "OFFSET": 3}
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 15
    assert marker.read_text().splitlines() == ["executed"]



def test_real_julia_bound_backend_change_uses_recorded_result(make_context, tmp_path, monkeypatch):
    import os
    import shutil
    import subprocess

    executable = shutil.which("julia")
    if executable is None:
        pytest.skip("Julia CLI executable is missing from PATH")
    monkeypatch.setenv("JULIA_PROJECT", os.environ.get("JULIA_PROJECT", "/tmp/seamless-julia-r-support-project"))
    probe = subprocess.run([executable, "--startup-file=no", "-e", "using NPZ, JSON"],
                           capture_output=True, text=True, timeout=180)
    if probe.returncode:
        pytest.skip("Julia CLI needs NPZ and JSON: " + probe.stderr[-1000:])
    marker = tmp_path / "julia-calls"
    tf = Transformer("julia")
    tf.metavars.BACKEND = "julia"
    tf.code = 'open(marker, "a") do io; println(io, "executed"); end; result = value + 2'
    tf.celltypes.value = "int"
    tf.pins.value = 12
    tf.celltypes.marker = "str"
    tf.pins.marker = str(marker)
    tf.celltypes.result = "plain"
    tf.local = True
    ctx = make_context()
    ctx.tf = tf
    ctx.compute(timeout=120)
    assert ctx.tf.result.value == 14
    checksum = ctx.tf.build().construct()
    ctx.tf.metavars.BACKEND = "juliacall"
    assert ctx.tf.build().construct() == checksum
    assert ctx.tf.metavars.BACKEND == "juliacall"
    assert ctx.tf.build().meta["metavars"]["BACKEND"] == "juliacall"
    ctx.compute(timeout=120)
    assert ctx.tf.result.value == 14
    assert marker.read_text().splitlines() == ["executed"]
