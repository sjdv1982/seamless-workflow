import pytest
from seamless import Buffer, Cell


def identity(value):
    return value


@pytest.mark.parametrize('source_kind', ['literal', 'checksum', 'cell', 'transformer'])
@pytest.mark.parametrize('setter', ['pin', 'transformer'])
@pytest.mark.parametrize('old_type,new_type,value,expected,next_value,next_expected', [
    ('int', 'int', 42, 42, 53, 53),
    ('int', 'float', 42, 42.0, 53, 53.0),
    ('str', 'int', '42', 42, '53', 53),
    ('str', 'text', 'hello', 'hello', 'world', 'world'),
    ('text', 'str', 'hello', 'hello', 'world', 'world'),
])
def test_pin_retype_matrix(make_context, source_kind, setter, old_type, new_type,
                           value, expected, next_value, next_expected):
    ctx = make_context()
    ctx.tf = identity
    ctx.tf.celltypes.value = old_type
    if source_kind == 'literal':
        ctx.tf.pins.value = value
    elif source_kind == 'checksum':
        ctx.tf.pins.value.set_checksum(Buffer(value, old_type).get_checksum())
    else:
        ctx.source = Cell(old_type)
        ctx.source.set(value)
        source = ctx.source
        if source_kind == 'transformer':
            ctx.upstream = identity
            ctx.upstream.celltypes.value = ctx.upstream.celltypes.result = old_type
            ctx.upstream.pins.value = ctx.source
            source = ctx.upstream
        ctx.tf.pins.value = source
    ctx.compute(timeout=10)
    pin = ctx.tf.pins.value
    assert pin.value == value
    if setter == 'pin': pin.celltype = new_type
    else: ctx.tf.celltypes.value = new_type
    ctx.compute(timeout=10)
    assert pin.input_celltype == old_type
    assert pin.state == 'complete', pin.exception
    assert pin.value == expected and type(pin.value) is type(expected)
    assert pin.checksum == Buffer(expected, new_type).get_checksum()
    assert pin.build().run() == expected
    assert ctx.tf.run() == ctx.tf().run()
    if source_kind in ('cell', 'transformer'):
        ctx.source.set(next_value)
    else:
        pin.set(next_expected)
    ctx.compute(timeout=10)
    assert pin.value == next_expected
    assert pin.checksum == Buffer(next_expected, new_type).get_checksum()
    pin.celltype = old_type
    ctx.compute(timeout=10)
    assert pin.value == next_value
