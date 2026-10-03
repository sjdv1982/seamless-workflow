"""Bound pin ownership from contracts/pins.md, *Scratch at the pin*."""

import uuid

import pytest

from seamless import Buffer, Cell, Checksum
from seamless.caching import buffer_writer
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.transformer import delayed
from seamless_workflow import Context


def add_suffix(value):
    return value + "-result"


@pytest.fixture
def writes(monkeypatch):
    result = []
    monkeypatch.setattr(buffer_writer, "register", lambda buf: result.append(buf.get_checksum()))
    return result


@pytest.mark.parametrize("allow_fingertip", [False, True])
def test_edge_fed_conversion_claim_follows_pin(writes, monkeypatch, allow_fingertip):
    """pins.md, *Scratch at the pin*: an edge-fed pin claims its converted input."""
    builder = delayed(add_suffix)
    builder.local = True
    ctx = Context()
    try:
        ctx.src = Cell("str")
        ctx.tf = builder
        ctx.tf.scratch = True
        ctx.tf.celltypes.value = "text"
        ctx.tf.allow_input_fingertip = allow_fingertip
        ctx.tf.pins.value = ctx.src
        value = "edge-" + uuid.uuid4().hex
        ctx.src.set(value)
        ctx.compute(timeout=20)
        assert ctx.tf.state == "complete", ctx.tf.exception
        converted_buffer = Buffer(value, "text")
        converted = converted_buffer.get_checksum()
        claims = dict((role, checksum) for checksum, role in ctx._refheld_checksums())
        assert claims["transformer:tf:pin:value"] == converted
        assert (converted in writes) is (not allow_fingertip)
        conversion_requests = [
            key for key in ctx._facts
            if key[0] == "expression" and key[3:5] == ("str", "text")
        ]
        assert conversion_requests
        assert all(key[-2:] == (allow_fingertip, not allow_fingertip)
                   for key in conversion_requests)
        cache = get_buffer_cache()
        assert cache.is_scratch_ref(converted) is allow_fingertip
        # A bound fingertip recovers bytes between runs. The Context claim
        # decides whether that recovered buffer is persisted.
        with cache.lock:
            cache.weak_cache.pop(converted, None)
            cache.strong_cache[converted].buffer = None
        writes.clear()
        original = Checksum.fingertip_sync

        def fingertip(self, celltype=None):
            if self == converted:
                cache.register(converted, converted_buffer)
                return converted_buffer
            return original(self, celltype)

        monkeypatch.setattr(Checksum, "fingertip_sync", fingertip)
        assert ctx.tf.pins.value.fingertip() is converted_buffer
        assert (converted in writes) is (not allow_fingertip)
    finally:
        ctx._release_refholds()
        builder._release_refholds()


def test_scratch_producer_dispatch_is_overruled_through_cell(monkeypatch):
    """pins.md, *Scratch at the pin*: a non-scratch pin overrules a producer."""
    dispatches = []
    original = Context._freeze_transformer

    def record(self, path, **kwargs):
        frozen = original(self, path, **kwargs)
        if kwargs.get("concrete_args") is not None:
            dispatches.append((path, frozen.scratch))
        return frozen

    monkeypatch.setattr(Context, "_freeze_transformer", record)
    producer = delayed(add_suffix)
    producer.local = True
    consumer = delayed(add_suffix)
    consumer.local = True
    ctx = Context()
    try:
        ctx.src = Cell("mixed")
        ctx.prod = producer
        ctx.prod.scratch = True
        ctx.prod.pins.value = ctx.src
        ctx.mid = ctx.prod
        ctx.cons = consumer
        ctx.cons.pins.value = ctx.mid
        ctx.src.set("dispatch-" + uuid.uuid4().hex)
        ctx.compute(timeout=20)
        assert ctx.cons.state == "complete", ctx.cons.exception
        assert (("prod",), False) in dispatches
    finally:
        ctx._release_refholds()
        producer._release_refholds()
        consumer._release_refholds()


@pytest.mark.parametrize("mid_scratch", [False, True])
@pytest.mark.parametrize("out_scratch", [False, True])
def test_non_scratch_cell_overrules_scratch_producer_dispatch(
    writes, monkeypatch, mid_scratch, out_scratch
):
    """cells.md, *Scratch policy*: in a Context, a non-scratch cell overrules a
    scratch transformer. "When a transformer's result feeds a non-scratch cell,
    directly or through other cells and Expressions, the Context dispatches that
    transformer non-scratch, whatever its own `scratch` ... Only the dispatch
    changes: the transformer node's own claim stays scratch."
    """
    dispatches = []
    original = Context._freeze_transformer

    def record(self, path, **kwargs):
        frozen = original(self, path, **kwargs)
        if kwargs.get("concrete_args") is not None:
            dispatches.append((path, frozen.scratch))
        return frozen

    monkeypatch.setattr(Context, "_freeze_transformer", record)
    producer = delayed(add_suffix)
    producer.local = True
    ctx = Context()
    try:
        ctx.src = Cell("mixed")
        ctx.prod = producer
        ctx.prod.scratch = True
        ctx.prod.pins.value = ctx.src
        ctx.mid = Cell("mixed")
        ctx.mid.scratch = mid_scratch
        ctx.mid = ctx.prod
        ctx.out = Cell("mixed")
        ctx.out.scratch = out_scratch
        ctx.out = ctx.mid
        assert ctx.mid.scratch is mid_scratch
        assert ctx.out.scratch is out_scratch
        ctx.src.set("cell-" + uuid.uuid4().hex)
        ctx.compute(timeout=20)
        assert ctx.out.state == "complete", ctx.out.exception
        dispatch_scratch = mid_scratch and out_scratch
        assert dispatches == [(("prod",), dispatch_scratch)]
        result = ctx._graph.nodes[("prod",)].current_checksum
        assert (result in writes) is (not dispatch_scratch)
    finally:
        ctx._release_refholds()
        producer._release_refholds()


@pytest.mark.parametrize("drop_buffer", [False, True])
def test_cell_made_non_scratch_after_a_scratch_run_gets_written_bytes(writes, drop_buffer):
    """cells.md, *Scratch policy*: "the cell holds a written buffer wherever the
    transformer ran". A completed scratch run whose bytes are gone runs again,
    non-scratch, once a cell it feeds becomes non-scratch."""
    producer = delayed(add_suffix)
    producer.local = True
    ctx = Context()
    try:
        ctx.src = Cell("mixed")
        ctx.prod = producer
        ctx.prod.scratch = True
        ctx.prod.pins.value = ctx.src
        ctx.out = Cell("mixed")
        ctx.out.scratch = True
        ctx.out = ctx.prod
        ctx.src.set("late-cell-" + uuid.uuid4().hex)
        ctx.compute(timeout=20)
        result = ctx._graph.nodes[("prod",)].current_checksum
        assert result is not None
        assert result not in writes
        generation = ctx._runtime.current_runs[("prod",)].generation
        cache = get_buffer_cache()
        if drop_buffer:
            with cache.lock:
                cache.weak_cache.pop(result, None)
                cache.strong_cache[result].buffer = None
        ctx.out.scratch = False
        ctx.compute(timeout=20)
        assert ctx.out.state == "complete", ctx.out.exception
        assert result in writes
        run = ctx._runtime.current_runs[("prod",)]
        assert (run.generation != generation) is drop_buffer
        if drop_buffer:
            assert run.dispatch_scratch is False
    finally:
        ctx._release_refholds()
        producer._release_refholds()


@pytest.mark.parametrize("scratch", [False, True])
def test_intermediate_link_into_cell_is_scratch(scratch):
    """An intermediate carries the ban; the cell's own link carries its policy."""
    ctx = Context()
    try:
        ctx.src = Cell("str")
        ctx.target = Cell("text")
        ctx.target.scratch = scratch
        ctx.target = ctx.src.as_celltype("plain").as_celltype("text")
        ctx.src.set("chain-" + uuid.uuid4().hex)
        ctx.compute(timeout=20)
        assert ctx.target.state == "complete", ctx.target.exception
        projections = [key for key in ctx._facts if key[0] == "expression"]
        first = [key for key in projections if key[3:5] == ("str", "plain")]
        last = [key for key in projections if key[3:5] == ("plain", "text")]
        assert len(first) == len(last) == 1, projections
        assert first[0][-2:] == (True, False)
        assert last[0][-2:] == (scratch, False)
    finally:
        ctx._release_refholds()


@pytest.mark.parametrize("drop_buffer", [False, True])
def test_completed_scratch_producer_is_recomputed_for_new_value_request(writes, drop_buffer):
    """pins.md, *Scratch at the pin*: a missing result's bytes require a new run."""
    producer = delayed(add_suffix)
    producer.local = True
    consumer = delayed(add_suffix)
    consumer.local = True
    ctx = Context()
    try:
        ctx.src = Cell("mixed")
        ctx.prod = producer
        ctx.prod.scratch = True
        ctx.prod.pins.value = ctx.src
        ctx.src.set("late-" + uuid.uuid4().hex)
        ctx.compute(timeout=20)
        result = ctx._graph.nodes[("prod",)].current_checksum
        assert result is not None
        generation = ctx._runtime.current_runs[("prod",)].generation
        cache = get_buffer_cache()
        if drop_buffer:
            with cache.lock:
                cache.weak_cache.pop(result, None)
                cache.strong_cache[result].buffer = None
        writes.clear()
        ctx.cons = consumer
        ctx.cons.pins.value = ctx.prod
        ctx.compute(timeout=20)
        assert ctx.cons.state == "complete", ctx.cons.exception
        assert result in writes
        assert (ctx._runtime.current_runs[("prod",)].generation != generation) is drop_buffer
    finally:
        ctx._release_refholds()
        producer._release_refholds()
        consumer._release_refholds()
