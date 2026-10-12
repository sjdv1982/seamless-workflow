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
