"""Contract tests for docs/agent/contracts/transformers.md (bound Transformer).

Target repo location: seamless-workflow/tests/test_contract_transformer_bound.py

Covers: fresh interchangeable views, binding-as-move, explicit build of a bound
Transformer as a detached snapshot that does not alter durable node state,
named work methods on the live node regardless of call mode, and the read-only
bound result.
"""

from __future__ import annotations

import asyncio

import pytest

from seamless_transformer import Transformer, delayed, direct
from seamless_transformer.transformation_class import Transformation
from seamless_workflow import Context
from seamless_workflow.errors import ReadOnlyEndpointError


def add(a, b):
    return a + b


def mul(a, b):
    return a * b


def test_every_access_returns_a_fresh_interchangeable_view(make_context):
    ctx = make_context()
    ctx.tf = add
    v1 = ctx.tf
    v2 = ctx.tf
    assert v1 is not v2
    v1.pins.a = 2
    v2.pins.b = 3
    ctx.compute(timeout=10)
    assert v1.result.value == v2.result.value == 5
    assert v1.state == v2.state == "complete"


def test_binding_moves_builder_state_and_old_handle_views_the_node(make_context):
    """transformers.md §Binding: binding is a move; workflow-context.md:
    "two handles for one node are deliberate aliases" (no dual-write window)."""
    ctx = make_context()
    tf = delayed(add)
    tf.pins.a = 1
    tf.pins.b = 1
    ctx.tf = tf

    tf.pins.b = 50  # mutation through the pre-binding handle reaches the node
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 51

    ctx.tf.celltypes.result = "text"  # and node mutations are visible on it
    assert tf.celltypes.result == "text"
    ctx.tf.code = mul
    ctx.compute(timeout=10)
    assert tf.state == "complete"
    assert tf.result.value == "50"


@pytest.mark.parametrize("alias", ["transformation", "get_transformation"])
def test_bound_explicit_build_is_a_detached_snapshot(make_context, alias):
    ctx = make_context()
    ctx.x = 2
    ctx.tf = add
    ctx.tf.pins.a = ctx.x
    ctx.tf.pins.b = 3
    ctx.compute(timeout=10)

    graph_before = ctx.get_graph()
    snapshot = getattr(ctx.tf, alias)()
    assert isinstance(snapshot, Transformation)
    assert ctx.get_graph() == graph_before

    ctx.x = 10
    ctx.tf.code = mul
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 30
    assert snapshot.run() == 5


@pytest.mark.parametrize("mode", ["delayed", "direct"])
def test_bound_build_returns_transformation_for_every_call_mode(make_context, mode):
    ctx = make_context()
    tf = (direct if mode == "direct" else delayed)(add)
    tf.pins.a = 2
    tf.pins.b = 3
    ctx.tf = tf
    for op in ("build", "transformation", "get_transformation"):
        snapshot = getattr(ctx.tf, op)()
        assert isinstance(snapshot, Transformation)
        assert snapshot.run() == 5


@pytest.mark.parametrize("mode", ["delayed", "direct"])
def test_bound_named_methods_target_the_live_node(make_context, mode):
    ctx = make_context()
    tf = (direct if mode == "direct" else delayed)(add)
    tf.pins.a = 2
    tf.pins.b = 3
    ctx.tf = tf
    assert type(ctx.tf).__name__ == type(tf).__name__

    checksum = ctx.tf.compute(timeout=10)
    assert ctx.tf.state == "complete"
    assert checksum == ctx.tf.result.checksum
    assert asyncio.run(ctx.tf.computation(timeout=10)) == checksum
    assert ctx.tf.run() == 5
    assert ctx.tf.prune() == {"cancelled": 0}
    assert ctx.tf.exception is None
    assert ctx.tf.block_reason is None
    for method in ("prune", "clear_exception"):
        assert hasattr(ctx.tf, method)


@pytest.mark.parametrize("mode", ["delayed", "direct"])
def test_bound_clear_exception_is_available(make_context, mode):
    """transformers.md §Named work methods: on a bound Transformer
    clear_exception() operates on the live node (its effect is owned by the
    node lifecycle contract; here only that it is callable)."""

    def boom(a):
        raise RuntimeError("boom")

    ctx = make_context()
    tf = (direct if mode == "direct" else delayed)(boom)
    tf.pins.a = 1
    ctx.tf = tf
    ctx.compute(timeout=10)
    assert ctx.tf.state == "failed"
    assert ctx.tf.exception is not None
    ctx.tf.clear_exception()


def test_bound_result_rejects_producer_operations(make_context):
    """transformers.md §Pins and result: any producer operation aimed at the
    result raises ReadOnlyEndpointError (method forms; attribute assignment is
    covered by ``test_bound_result_cannot_be_assigned``)."""
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.a = 1
    ctx.tf.pins.b = 2
    ctx.compute(timeout=10)
    checksum = ctx.tf.result.checksum
    with pytest.raises(ReadOnlyEndpointError):
        ctx.tf.result.set(5)
    with pytest.raises(ReadOnlyEndpointError):
        ctx.tf.result.set_checksum(checksum)
    with pytest.raises(ReadOnlyEndpointError):
        ctx.tf.result.set_buffer(b"5")
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 3


_COMPILED_SCHEMA = """\
inputs:
  - {name: a, dtype: int32}
  - {name: b, dtype: int32}
outputs:
  - {name: result, dtype: int32}
"""


def test_bound_compiled_build_is_a_detached_transformation(make_context):
    """transformers.md §Building a Transformation: build() exists on every
    builder, compiled and bound included; directness does not change identity."""
    ctx = make_context()
    identities = []
    for d in (False, True):
        tf = Transformer("c", compiled=True, direct=d)
        tf.schema = _COMPILED_SCHEMA
        tf.code = "int transform(int a, int b, int *result) { *result = a + b; return 0; }"
        tf.pins.a = 1
        tf.pins.b = 2
        name = "tf_direct" if d else "tf_delayed"
        setattr(ctx, name, tf)
        for op in ("build", "transformation", "get_transformation"):
            snapshot = getattr(getattr(ctx, name), op)()
            assert isinstance(snapshot, Transformation)
            snapshot.construct()
            identities.append(snapshot.transformation_checksum)
        assert snapshot.run() == 3
    assert len(set(identities)) == 1


def test_bound_result_cannot_be_assigned(make_context):
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.a = 1
    ctx.tf.pins.b = 2
    ctx.x = 99
    with pytest.raises(ReadOnlyEndpointError):
        ctx.tf.result = ctx.x
    with pytest.raises(ReadOnlyEndpointError):
        ctx.tf.result = 5
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 3


def test_bound_task_returns_asyncio_task(make_context):
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.a = 2
    ctx.tf.pins.b = 3
    ctx.compute(timeout=10)

    async def main():
        task = ctx.tf.task()
        try:
            assert isinstance(task, asyncio.Task)
        finally:
            if asyncio.iscoroutine(task):
                task.close()
            elif isinstance(task, asyncio.Task):
                task.cancel()

    asyncio.run(main())
