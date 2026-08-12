from __future__ import annotations

import gc
import weakref
from uuid import uuid4

from seamless.caching.buffer_cache import get_buffer_cache
from seamless_workflow import Context


def identity(value):
    return value


def _normal_refs(checksum):
    entry = get_buffer_cache().strong_cache.get(checksum)
    return 0 if entry is None else entry.normal_refs


def test_workflow_gc_releases_cell_and_transformer_pin_holds():
    payload = {"token": uuid4().hex}
    ctx = Context()
    ctx.value = payload
    ctx.identity = identity
    ctx.identity.pins.value = payload

    cell_checksum = ctx._graph.nodes[("value",)].cell_root_producer.checksum
    pin_checksum = ctx._graph.nodes[("identity",)].transformer_pin_producers[
        "value"
    ].checksum
    assert cell_checksum == pin_checksum
    assert _normal_refs(cell_checksum) == 2

    context_ref = weakref.ref(ctx)
    del ctx
    gc.collect()

    assert context_ref() is None
    assert _normal_refs(cell_checksum) == 0


def test_replacement_and_deletion_release_producer_holds():
    ctx = Context()
    ctx.value = {"first": uuid4().hex}
    first_cell = ctx._graph.nodes[("value",)].cell_root_producer.checksum
    ctx.value = {"second": uuid4().hex}
    second_cell = ctx._graph.nodes[("value",)].cell_root_producer.checksum

    assert _normal_refs(first_cell) == 0
    assert _normal_refs(second_cell) == 1

    ctx.identity = identity
    ctx.identity.pins.value = {"first-pin": uuid4().hex}
    first_pin = ctx._graph.nodes[("identity",)].transformer_pin_producers[
        "value"
    ].checksum
    ctx.identity.pins.value = {"second-pin": uuid4().hex}
    second_pin = ctx._graph.nodes[("identity",)].transformer_pin_producers[
        "value"
    ].checksum

    assert _normal_refs(first_pin) == 0
    assert _normal_refs(second_pin) == 1

    del ctx.identity.pins.value
    del ctx.value
    assert _normal_refs(second_pin) == 0
    assert _normal_refs(second_cell) == 0


def test_connections_release_replaced_literal_producers():
    ctx = Context()
    ctx.source = {"source": uuid4().hex}
    ctx.target = {"target": uuid4().hex}
    target_checksum = ctx._graph.nodes[("target",)].cell_root_producer.checksum

    ctx.target = ctx.source
    assert _normal_refs(target_checksum) == 0

    ctx.identity = identity
    ctx.identity.pins.value = {"pin": uuid4().hex}
    pin_checksum = ctx._graph.nodes[("identity",)].transformer_pin_producers[
        "value"
    ].checksum
    ctx.identity.pins.value = ctx.source
    assert _normal_refs(pin_checksum) == 0


def test_graph_load_and_subcontext_copy_adopt_independent_holds():
    ctx = Context()
    ctx.sub = Context()
    ctx.sub.value = {"token": uuid4().hex}
    checksum = ctx._graph.nodes[("sub", "value")].cell_root_producer.checksum
    assert _normal_refs(checksum) == 1

    ctx.copy = ctx.sub
    assert _normal_refs(checksum) == 2

    clone = Context()
    clone.set_graph(ctx.get_graph())
    assert _normal_refs(checksum) == 4

    del clone
    gc.collect()
    assert _normal_refs(checksum) == 2

    del ctx
    gc.collect()
    assert _normal_refs(checksum) == 0


def test_workflow_gc_reports_cache_refcount_underflow(capsys):
    ctx = Context()
    ctx.value = {"token": uuid4().hex}
    checksum = ctx._graph.nodes[("value",)].cell_root_producer.checksum
    assert checksum.decref() is True
    capsys.readouterr()

    context_ref = weakref.ref(ctx)
    del ctx
    gc.collect()

    assert context_ref() is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR: buffer cache refcount already zero" in captured.err
    assert checksum.hex() in captured.err
