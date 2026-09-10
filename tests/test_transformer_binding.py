from __future__ import annotations

import pytest

from seamless_transformer.transformer_class import delayed

from seamless_workflow import Context


def add(x, y):
    return x + y


def default_argument(a=100):
    return a


@pytest.mark.parametrize("binding", ["callable", "builder"])
def test_default_argument_completes_without_wiring(make_context, binding):
    ctx = make_context()
    ctx.func = default_argument if binding == "callable" else delayed(default_argument)
    ctx.compute(timeout=10)

    assert ctx.func.state == "complete"
    assert ctx.func.result.value == 100

    ctx.func.pins.a = 200
    ctx.compute(timeout=10)

    assert ctx.func.state == "complete"
    assert ctx.func.result.value == 200


def test_standalone_transformer_pins_let_call_omit_prebound_arguments():
    tf = delayed(add)
    tf.pins.x = 10

    result = tf(y=5).run()
    assert result == 15


def test_binding_transformer_moves_prebound_pins_to_context():
    tf = delayed(add)
    tf.pins.x = 10

    ctx = Context()
    ctx.tf = tf
    assert tf.pins.x == 10

    tf.pins.y = 7
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 17

    ctx.tf.pins.x = 3
    assert tf.pins.x == 3
    assert tf().run() == 10


def test_bound_transformer_call_arguments_override_pins_for_call_only():
    tf = delayed(add)
    tf.pins.x = 10
    tf.pins.y = 1

    ctx = Context()
    ctx.tf = tf

    assert tf(y=5).run() == 15
    assert tf.pins.y == 1
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 11


def test_function_assignment_creates_reactive_transformer():
    ctx = Context()
    ctx.add = add
    ctx.add.pins.x = 2
    ctx.add.pins.y = 4
    ctx.compute(timeout=10)

    assert ctx.add.result.value == 6
