import pytest
from seamless import Buffer, Cell


def identity(value):
    return value


def _current_run_identity(ctx, name):
    node, = [node for node in ctx.get_graph(runtime=True)['nodes'] if node['path'] == [name]]
    return node['runtime']['run']['current']['identity']


@pytest.mark.parametrize('source_kind', ['literal', 'checksum', 'cell', 'transformer'])
@pytest.mark.parametrize('setter', ['pin', 'transformer'])
@pytest.mark.parametrize('old_type,new_type,value,expected,next_value,next_expected,keeps_checksum', [
    ('int', 'int', 42, 42, 53, 53, True),
    ('int', 'float', 42, 42.0, 53, 53.0, True),
    ('int', 'str', 42, '42', 53, '53', True),
    ('str', 'int', '42', 42, '53', 53, True),
    ('text', 'int', '42', 42, '53', 53, True),
    ('str', 'text', 'hello', 'hello', 'world', 'world', False),
    ('text', 'str', 'hello', 'hello', 'world', 'world', False),
])
def test_pin_retype_matrix(make_context, source_kind, setter, old_type, new_type,
                           value, expected, next_value, next_expected, keeps_checksum):
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
    # A conversion between nested celltypes keeps the checksum; a reformat makes a new one.
    if keeps_checksum:
        assert pin.checksum == Buffer(value, old_type).get_checksum()
    else:
        assert pin.checksum == Buffer(expected, new_type).get_checksum()
    assert pin.build().run() == expected
    # The pin's checksum is what enters the transformation, reactively and in a snapshot.
    transformation = ctx.tf().construct()
    assert transformation.resolve('plain')['value'] == [new_type, None, pin.checksum.hex()]
    assert _current_run_identity(ctx, 'tf') == transformation.hex()
    assert ctx.tf.run() == ctx.tf().run()
    if source_kind in ('cell', 'transformer'):
        ctx.source.set(next_value)
    else:
        pin.set(next_expected)
    ctx.compute(timeout=10)
    assert pin.value == next_expected
    if keeps_checksum and source_kind in ('cell', 'transformer'):
        assert pin.checksum == Buffer(next_value, old_type).get_checksum()
    else:
        assert pin.checksum == Buffer(next_expected, new_type).get_checksum()
    pin.celltype = old_type
    ctx.compute(timeout=10)
    assert pin.value == next_value
