import pytest
from seamless import Cell
from seamless_transformer import delayed
from seamless_workflow import Context


def defaults(a=3, *args, b=4, **kwargs):
    return [a, b]


def test_bound_optional_view_and_reactive_toggle():
    builder = delayed(defaults)
    builder.optional_pins.a.disable()
    with Context() as ctx:
        ctx.tf = builder
        view = ctx.tf.optional_pins
        assert view == {'b'} and 'a' in dir(view)
        assert 'args' not in dir(ctx.tf.pins) and 'kwargs' not in dir(ctx.tf.pins)
        ctx.compute()
        assert ctx.tf.state == 'unwired'
        with pytest.raises(TypeError, match='a'):
            ctx.tf()
        with pytest.raises(AttributeError):
            ctx.tf.optional_pins = {'a'}
        view.a.enable()
        ctx.compute()
        assert ctx.tf.run() == ctx.tf().run() == [3, 4]
        absent = ctx.tf().construct()
        ctx.source = Cell('plain')
        ctx.source.set(None)
        ctx.tf.pins.a = ctx.source
        ctx.compute()
        assert ctx.tf().construct() == absent
        view.a.disable()
        ctx.compute()
        assert ctx.tf.run() == ctx.tf().run() == [None, 4]
        assert ctx.tf().construct() != absent
        ctx.tf.celltypes.a = 'int'
        ctx.compute()
        assert ctx.tf.state == 'blocked'
        assert isinstance(ctx.tf.pins.a.exception, str)
        assert ('tf',) not in ctx._runtime.current_runs
        view.a.enable()
        ctx.compute()
        assert ctx.tf.run() == [3, 4]
        ctx.source.checksum = None
        ctx.compute()
        assert ctx.tf.state == 'blocked'


def test_replacing_bound_code_derives_new_defaults():
    def replacement(c=9):
        return c

    with Context() as ctx:
        ctx.tf = defaults
        view = ctx.tf.optional_pins
        view.a.disable()
        ctx.tf.code = replacement
        ctx.compute()
        assert view == {'c'}
        assert 'a' not in dir(view)
        assert ctx.tf.run() == 9


def test_pin_projection_wiring_and_mixed_block_reasons():
    with Context() as ctx:
        ctx.source = Cell('plain')
        ctx.source.set([2])
        ctx.tf = defaults
        ctx.tf.celltypes.a = 'int'
        with pytest.raises(TypeError, match='as_celltype'):
            ctx.tf.pins.a = ctx.source[0]
        ctx.tf.celltypes.a = 'plain'
        ctx.tf.pins.a = ctx.source[0]
        with pytest.raises(TypeError, match='as_celltype'):
            ctx.tf.pins.a.celltype = 'int'
        ctx.compute()
        assert ctx.tf.run() == [2, 4]
        ctx.source.celltype = 'mixed'
        ctx.tf.optional_pins.b.disable()
        ctx.compute()
        assert ctx.tf.pins.a.state == ctx.tf.state == 'miswired'
        assert ctx.tf.block_reason == {'a': 'miswired', 'b': 'unwired'}
        ctx.tf.block_reason.clear()
        assert ctx.tf.block_reason == {'a': 'miswired', 'b': 'unwired'}


def test_bound_pin_materialization_error_and_clear(monkeypatch):
    from seamless import Checksum
    with Context() as ctx:
        ctx.tf = defaults
        ctx.tf.pins.a = 3
        ctx.compute()
        pin = ctx.tf.pins.a
        original = Checksum.resolve
        def fail(checksum, *args, **kwargs):
            raise ValueError('cannot decode pin')
        monkeypatch.setattr(Checksum, 'resolve', fail)
        with pytest.raises(ValueError, match='cannot decode pin'):
            pin.buffer
        assert pin.exception == 'cannot decode pin'
        assert ctx.tf.block_reason == {'a': 'blocked-by-error'}
        monkeypatch.setattr(Checksum, 'resolve', original)
        pin.clear_exception()
        ctx.compute()
        assert pin.exception is None and pin.value == 3
