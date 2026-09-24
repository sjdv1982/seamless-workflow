"""Contract tests for docs/agent/contracts/transformers.md (bound Transformer).

Target repo location: seamless-workflow/tests/test_contract_transformer_bound.py

Covers: fresh interchangeable views, binding-as-move, explicit build of a bound
Transformer as a detached snapshot that does not alter durable node state,
named work methods on the live node regardless of call mode, and the read-only
bound result.
"""

from __future__ import annotations

import pytest

from seamless_transformer import delayed, direct
from seamless_transformer.transformation_class import Transformation
from seamless_workflow import Context


BUILD_AHEAD = (
    "transformers.md §Contract change: a mode-independent build operation: "
    "no build(); transformation() is self(), so a bound direct Transformer "
    "returns a value"
)


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


@pytest.mark.xfail(strict=False, reason=BUILD_AHEAD)
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
    assert ctx.tf.run() == 5
    assert ctx.tf.prune() == {"cancelled": 0}
    assert ctx.tf.exception is None
    assert ctx.tf.block_reason is None


def test_bound_result_cannot_be_assigned(make_context):
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.a = 1
    ctx.tf.pins.b = 2
    ctx.x = 99
    with pytest.raises(Exception):
        ctx.tf.result = ctx.x
    with pytest.raises(Exception):
        ctx.tf.result = 5
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 3
