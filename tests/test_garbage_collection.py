from __future__ import annotations

import gc
import weakref
from uuid import uuid4

from seamless.caching.buffer_cache import get_buffer_cache
from seamless_workflow import Context


def identity(value):
    return value


def _refholder_refs(checksum):
    return get_buffer_cache().reference_snapshot().get(checksum, (0, 0, False))[0]


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
    assert _refholder_refs(cell_checksum) == 4

    context_ref = weakref.ref(ctx)
    del ctx
    gc.collect()

    assert context_ref() is None
    assert _refholder_refs(cell_checksum) == 0


def test_replacement_and_deletion_release_producer_holds():
    ctx = Context()
    ctx.value = {"first": uuid4().hex}
    first_cell = ctx._graph.nodes[("value",)].cell_root_producer.checksum
    ctx.value = {"second": uuid4().hex}
    second_cell = ctx._graph.nodes[("value",)].cell_root_producer.checksum

    assert _refholder_refs(first_cell) == 1
    assert _refholder_refs(second_cell) == 2

    ctx.identity = identity
    ctx.identity.pins.value = {"first-pin": uuid4().hex}
    first_pin = ctx._graph.nodes[("identity",)].transformer_pin_producers[
        "value"
    ].checksum
    ctx.identity.pins.value = {"second-pin": uuid4().hex}
    second_pin = ctx._graph.nodes[("identity",)].transformer_pin_producers[
        "value"
    ].checksum

    assert _refholder_refs(first_pin) == 1
    assert _refholder_refs(second_pin) == 2

    del ctx.identity.pins.value
    del ctx.value
    assert _refholder_refs(second_pin) == 1
    ctx.identity.prune()
    assert _refholder_refs(second_pin) == 0
    assert _refholder_refs(second_cell) == 0


def test_connections_release_replaced_literal_producers():
    ctx = Context()
    ctx.source = {"source": uuid4().hex}
    ctx.target = {"target": uuid4().hex}
    target_checksum = ctx._graph.nodes[("target",)].cell_root_producer.checksum

    ctx.target = ctx.source
    assert _refholder_refs(target_checksum) == 1

    ctx.identity = identity
    ctx.identity.pins.value = {"pin": uuid4().hex}
    pin_checksum = ctx._graph.nodes[("identity",)].transformer_pin_producers[
        "value"
    ].checksum
    ctx.identity.pins.value = ctx.source
    assert _refholder_refs(pin_checksum) == 1


def test_graph_load_and_subcontext_copy_adopt_independent_holds():
    ctx = Context()
    ctx.sub = Context()
    ctx.sub.value = {"token": uuid4().hex}
    checksum = ctx._graph.nodes[("sub", "value")].cell_root_producer.checksum
    assert _refholder_refs(checksum) == 2

    ctx.copy = ctx.sub
    assert _refholder_refs(checksum) == 4

    clone = Context()
    clone.set_graph(ctx.get_graph())
    assert _refholder_refs(checksum) == 8

    del clone
    gc.collect()
    assert _refholder_refs(checksum) == 4

    del ctx
    gc.collect()
    assert _refholder_refs(checksum) == 0


def test_workflow_gc_reports_cache_refcount_underflow(caplog):
    ctx = Context()
    ctx.value = {"token": uuid4().hex}
    checksum = ctx._graph.nodes[("value",)].cell_root_producer.checksum
    assert checksum.decref_refholder() is True
    caplog.clear()

    context_ref = weakref.ref(ctx)
    del ctx
    gc.collect()

    assert context_ref() is None
    assert "Refholder decref ignored" in caplog.text
