from __future__ import annotations

from seamless_transformer.transformer_class import delayed

from seamless_workflow import Context


def add(x, y):
    return x + y


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
    assert ctx.tf.result.value == 17

    ctx.tf.pins.x = 3
    assert tf.pins.x == 3
    assert tf() == 10


def test_function_assignment_creates_eager_transformer():
    ctx = Context()
    ctx.add = add
    ctx.add.pins.x = 2
    ctx.add.pins.y = 4

    assert ctx.add.result.value == 6
