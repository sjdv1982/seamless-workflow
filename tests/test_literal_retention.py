from __future__ import annotations

import gc

from seamless import CacheMissError
from seamless.caching.buffer_cache import get_buffer_cache
from seamless_workflow import Context
from seamless_workflow import adapters


def add_length(a, z):
    return a + len(z)


def test_transformer_literal_survives_tempref_expiry():
    ctx = Context()
    ctx.add_length = add_length
    payload = "workflow-pin-retention-" * 1003
    ctx.add_length.pins.z = payload

    producer = ctx._graph.nodes[("add_length",)].transformer_pin_producers["z"]
    cache = get_buffer_cache()
    buffer = cache.get(producer.checksum)
    assert buffer is not None

    # Reproduce the old failure mode: remove the historical process-global
    # buffer hold, expire the checksum's tempref, and run cache cleanup.
    getattr(adapters, "_LOCAL_BUFFERS", {}).pop(producer.checksum.hex(), None)
    tempref = buffer.tempref(interest=1e-12, fade_interval=1.0, scratch=True)
    tempref.clear()
    del buffer
    gc.collect()
    cache.run_eviction_once()
    gc.collect()

    ctx.add_length.pins.a = 20
    assert ctx.add_length.status == "Status: OK"
    assert ctx.add_length.exception is None
    assert ctx.add_length.result.value == 20 + len(payload)


def test_unavailable_literal_is_captured_as_transformer_exception(capsys):
    ctx = Context()
    ctx.add_length = add_length
    ctx.add_length.pins.a = 20
    graph = ctx.get_graph()
    transformer = next(node for node in graph["nodes"] if node["type"] == "transformer")
    transformer["producers"]["z"] = {
        "checksum": "f" * 64,
        "celltype": "mixed",
    }

    restored = Context()
    restored.set_graph(graph)

    assert restored.add_length.status == "Status: error"
    assert isinstance(restored.add_length.exception, CacheMissError)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
