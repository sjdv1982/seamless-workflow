"""Pin diagnostics collect all inputs matching the current pending reason."""
from itertools import permutations
from types import SimpleNamespace

import pytest

from seamless_workflow.builder_state import BoundTransformerBackend
from seamless_workflow.graph import Node, TransformerConfig
from seamless_workflow.reactive import Reactive


def add(x, y):
    return x + y


def test_unwired_pins_update_and_diagnostics_are_detached(make_context):
    ctx = make_context()
    ctx.tf = add
    assert ctx.tf.block_reason == ["x", "y"]
    ctx.tf.block_reason.clear()
    assert ctx.tf.block_reason == ["x", "y"]
    ctx.tf.pins.x = 1
    assert ctx.tf.block_reason == ["y"]
    ctx.tf.pins.y = 2
    ctx.compute(timeout=10)
    assert ctx.tf.block_reason is None


def test_multiple_upstream_unwired_pins(make_context):
    ctx = make_context()
    ctx.source = add
    ctx.tf = add
    ctx.tf.pins.x = ctx.source
    ctx.tf.pins.y = ctx.source
    assert ctx.tf.block_reason == ["x", "y"]


@pytest.mark.parametrize("source_state, reason", [
    ("computing", "waiting"),
    ("waiting", "waiting"),
    ("failed", "blocked-by-error"),
    ("unwired", "blocked-by-unwired"),
    ("blocked", "blocked-by-unwired"),
])
def test_collects_code_and_all_pins(source_state, reason):
    # Controlled upstream states avoid racing real background computations.
    node = Node("transformer", transformer_config=TransformerConfig(pins={"x", "y"}))
    incoming = {(pin,): source_state for pin in ("code", "x", "y")}
    context = SimpleNamespace(
        _incoming_for=lambda path: incoming,
        _source_state=lambda edge: (edge, None),
        _suspend=lambda path: None,
        _replace_current_checksum=lambda path, checksum: None,
    )
    Reactive._derive_transformer(context, ("tf",), node)
    assert node.state == ("waiting" if reason == "waiting" else "blocked")
    assert node.block_reason == (None if reason == "waiting" else reason)
    assert node.block_pins == ["code", "x", "y"]
    backend = SimpleNamespace(_node=lambda: node)
    assert BoundTransformerBackend.block_reason.fget(backend) == ["code", "x", "y"]


def test_optional_missing_pins_are_omitted():
    node = Node("transformer", transformer_config=TransformerConfig(
        pins={"x", "y"}, optional_pins={"y"}
    ))
    context = SimpleNamespace(
        _incoming_for=lambda path: {},
        _suspend=lambda path: None,
        _replace_current_checksum=lambda path, checksum: None,
    )
    Reactive._derive_transformer(context, ("tf",), node)
    assert node.state == "unwired"
    assert node.block_pins == ["code", "x"]


def test_blocked_pins_include_both_errors_and_missing_upstream_inputs():
    node = Node("transformer", transformer_config=TransformerConfig(pins={"x", "y"}))
    incoming = {("code",): "failed", ("x",): "unwired", ("y",): "computing"}
    context = SimpleNamespace(
        _incoming_for=lambda path: incoming,
        _source_state=lambda edge: (edge, None),
        _suspend=lambda path: None,
        _replace_current_checksum=lambda path, checksum: None,
    )
    Reactive._derive_transformer(context, ("tf",), node)
    assert node.state == "blocked"
    assert node.block_pins == ["code", "x"]


@pytest.mark.parametrize("input_states", list(permutations([
    "missing", "failed", "unwired", "computing"
])))
def test_mixed_inputs_follow_state_precedence_regardless_of_pin_order(input_states):
    names = ("code", "x", "y", "z")
    incoming = {(name,): state for name, state in zip(names, input_states)
                if state != "missing"}
    node = Node("transformer", transformer_config=TransformerConfig(pins=set(names[1:])))
    context = SimpleNamespace(
        _incoming_for=lambda path: incoming,
        _source_state=lambda edge: (edge, None),
        _suspend=lambda path: None,
        _replace_current_checksum=lambda path, checksum: None,
    )
    backend = SimpleNamespace(_node=lambda: node)

    Reactive._derive_transformer(context, ("tf",), node)
    assert node.state == "unwired"
    missing_pin = names[input_states.index("missing")]
    assert BoundTransformerBackend.block_reason.fget(backend) == [missing_pin]

    # Wiring the missing pin reveals all blocked inputs, including errors.
    incoming[(missing_pin,)] = "computing"
    Reactive._derive_transformer(context, ("tf",), node)
    assert node.state == "blocked"
    assert node.block_reason == "blocked-by-error"
    assert BoundTransformerBackend.block_reason.fget(backend) == sorted(
        name for name, state in zip(names, input_states) if state in {"failed", "unwired"}
    )

    # Once those inputs can progress, all inputs are waiting.
    incoming.update({(name,): "computing" for name in names})
    Reactive._derive_transformer(context, ("tf",), node)
    assert node.state == "waiting"
    assert BoundTransformerBackend.block_reason.fget(backend) == list(names)
