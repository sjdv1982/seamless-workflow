import pytest
from seamless import AuthorityError, Buffer, Cell, Expression
from seamless_transformer import Pin, delayed
from seamless_workflow import Context


def identity(value):
    return value


def test_bound_pin_converts_and_reports_source():
    with Context() as ctx:
        ctx.source = Cell('str')
        ctx.source.set('hello')
        ctx.tf = identity
        ctx.tf.celltypes.value = 'text'
        ctx.tf.celltypes.result = 'text'
        ctx.tf.pins.value = ctx.source
        ctx.compute()
        pin = ctx.tf.pins.value
        assert isinstance(pin, Pin) and not isinstance(pin, Cell)
        assert pin is not ctx.tf.pins.value
        assert pin.input_celltype == 'str' and pin.celltype == 'text'
        assert pin.source._workflow_endpoint() == ctx.source._workflow_endpoint()
        assert pin.value == pin.run() == pin.build().run() == 'hello'
        assert pin.checksum == Buffer('hello', 'text').get_checksum()
        assert ctx.tf.run() == ctx.tf().run() == 'hello'


def test_constant_retype_and_failure_blocks_construction():
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = 'str'
        ctx.tf.pins.value = 'hello'
        ctx.compute()
        pin = ctx.tf.pins.value
        pin.celltype = 'int'
        ctx.compute()
        assert pin.input_celltype == 'str'
        assert pin.state == 'failed' and pin.exception is not None
        assert ctx.tf.state == 'blocked'
        # A transformer's block_reason lists the culprit pins; its result carries the kind.
        assert ctx.tf.block_reason == {'value': 'blocked-by-error'}
        assert ctx.tf.result.block_reason == 'blocked-by-error'
        assert ctx.tf.result.checksum is None
        assert ('tf',) not in ctx._runtime.current_runs
        pin.celltype = 'text'
        ctx.compute()
        assert pin.value == 'hello'
        assert ctx.tf.run() == ctx.tf().run() == 'hello'


@pytest.mark.parametrize('form', ['value', 'buffer', 'checksum', 'pins'])
def test_writes_check_or_detach(form):
    with Context() as ctx:
        ctx.source = Cell('int')
        ctx.source.set(10)
        ctx.tf = identity
        ctx.tf.celltypes.value = 'int'
        ctx.tf.pins.value = ctx.source
        ctx.compute()
        pin = ctx.tf.pins.value
        value = 12 if form in ('value', 'pins') else Buffer(12, 'int')
        if form == 'checksum': value = value.get_checksum()
        method = 'set' if form in ('value', 'pins') else 'set_' + form
        with pytest.raises(AuthorityError): getattr(pin, method)(value)
        if form == 'pins': ctx.tf.pins.value = value
        else: setattr(pin, form, value)
        ctx.compute()
        assert pin.source is None and pin.value == 12
        assert ctx.get_graph()['connections'] == []
        assert pin.checksum == Buffer(12, 'int').get_checksum()
        pin.checksum = None
        ctx.compute()
        assert pin.state == 'unwired' and pin.input_celltype is None
        with pytest.raises(AttributeError): del ctx.tf.pins.value


def test_pins_cannot_be_sources():
    with Context() as ctx:
        ctx.source = 4
        ctx.tf = identity
        ctx.tf.pins.value = ctx.source
        ctx.tf2 = identity
        for action in [lambda: setattr(ctx, 'other', ctx.tf.pins.value),
                       lambda: setattr(ctx.tf2.pins, 'value', ctx.tf.pins.value),
                       lambda: Cell(source=ctx.tf.pins.value),
                       lambda: Expression(ctx.tf.pins.value)]:
            with pytest.raises(TypeError, match="Pin can't be a source.*pin.source"):
                action()
        ctx.tf2.pins.value = ctx.tf.pins.value.source
        ctx.compute()
        assert ctx.tf2.run() == 4


def test_unset_and_signatureless_deletion():
    with Context() as ctx:
        ctx.tf = identity
        assert isinstance(ctx.tf.pins.value, Pin)
        assert ctx.tf.pins.value.state == 'unwired'
        ctx.loose = delayed('result = value')
        ctx.loose.pins.value = None
        assert ctx.loose.pins.value.checksum == Buffer(None, 'plain').get_checksum()
        ctx.loose.celltypes.value = 'int'
        with pytest.raises(AttributeError):
            ctx.loose.optional_pins = {'value'}
        del ctx.loose.pins.value
        with pytest.raises(AttributeError): ctx.loose.pins.value
        # Deleting the declaration also drops its celltype and optionality.
        assert ctx.loose.optional_pins == set()
        with pytest.raises(AttributeError): ctx.loose.celltypes.value
        node, = [node for node in ctx.get_graph()['nodes'] if node['path'] == ['loose']]
        assert node['pins'] == {} and node['optional_pins'] == []


def optional_identity(value=None):
    return value


@pytest.mark.parametrize('celltype', ['int', 'binary', 'bytes', 'plain'])
def test_optional_null_is_dropped_before_conversion(celltype):
    with Context() as ctx:
        ctx.source = Cell('int')
        ctx.source.set(None)
        ctx.tf = optional_identity
        ctx.tf.celltypes.value = celltype
        assert ctx.tf.optional_pins == {'value'}
        ctx.tf.pins.value = ctx.source
        ctx.compute()
        assert ctx.tf.pins.value.state == 'complete'
        assert ctx.tf.pins.value.checksum == Buffer(None, 'plain').get_checksum()
        assert ctx.tf.state == 'complete', ctx.tf.exception
        assert 'value' not in ctx.tf().construct().resolve('plain')
        ctx.source.checksum = None
        ctx.compute()
        assert ctx.tf.pins.value.state == 'blocked'
        assert ctx.tf.state == 'blocked'
        assert ctx.tf.result.checksum is None


def returns_none():
    return None


def test_connected_none_result_drops_optional_pin():
    with Context() as ctx:
        ctx.up = returns_none
        ctx.up.celltypes.result = 'plain'
        ctx.tf = optional_identity
        ctx.tf.celltypes.value = 'int'
        ctx.tf.celltypes.result = 'plain'
        assert ctx.tf.optional_pins == {'value'}
        ctx.tf.pins.value = ctx.up
        ctx.compute()
        assert ctx.up.result.checksum == Buffer(None, 'plain').get_checksum()
        assert ctx.tf.pins.value.state == 'complete'
        assert ctx.tf.state == 'complete', ctx.tf.exception
        assert ctx.tf.result.value is None
        assert 'value' not in ctx.tf().construct().resolve('plain')


def test_required_null_is_a_pin_error_and_literal_rejected():
    with Context() as ctx:
        ctx.source = Cell('int')
        ctx.source.set(None)
        ctx.tf = identity
        ctx.tf.celltypes.value = 'int'
        with pytest.raises(TypeError, match="Required pin 'value'.*int"):
            ctx.tf.pins.value = None
        ctx.tf.pins.value = ctx.source
        ctx.compute()
        assert ctx.tf.pins.value.state == 'failed'
        assert 'int' in str(ctx.tf.pins.value.exception)
        assert ctx.tf.state == 'blocked'
        assert ctx.tf.block_reason == {'value': 'blocked-by-error'}
        assert ctx.tf.result.block_reason == 'blocked-by-error'
        assert ('tf',) not in ctx._runtime.current_runs


def test_declared_checksum_roundtrip_preserves_pin_input_type():
    with Context() as ctx, Context() as restored:
        ctx.tf = identity
        ctx.tf.celltypes.value = 'str'
        # A declaration doesn't deposit the buffer, so hold it.
        buffer = Buffer(42, 'int')
        buffer.tempref()
        ctx.tf.pins.value.set_checksum(buffer.get_checksum(), input_celltype='int')
        ctx.compute()
        assert ctx.tf.pins.value.input_celltype == 'int'
        assert ctx.tf.pins.value.value == '42'
        restored.set_graph(ctx.get_graph())
        restored.compute()
        assert restored.tf.pins.value.input_celltype == 'int'
        assert restored.tf.pins.value.value == '42'


@pytest.mark.parametrize('replace', [False, True])
def test_binding_retyped_builder_keeps_original_pin_input(replace):
    tf = delayed(identity)
    tf.celltypes.value = 'str'
    tf.pins.value = '42'
    tf.pins.value.celltype = 'int'
    with Context() as ctx:
        if replace:
            ctx.tf = identity
            ctx.tf.pins.value = 17
        ctx.tf = tf
        ctx.compute()
        assert ctx.tf.pins.value.input_celltype == 'str'
        assert ctx.tf.pins.value.value == 42
        ctx.tf.pins.value.celltype = 'text'
        ctx.compute()
        assert ctx.tf.pins.value.value == '42'


def test_pin_input_type_readonly_and_stale_handle():
    with Context() as ctx:
        ctx.tf = identity
        pin = ctx.tf.pins.value
        with pytest.raises(AttributeError): pin.input_celltype = 'int'
        del ctx.tf
        from seamless_workflow.errors import StaleWorkflowHandleError
        with pytest.raises(StaleWorkflowHandleError): pin.value


def test_empty_bytes_checksum_canonicalizes_before_optional_drop():
    with Context() as ctx:
        ctx.tf = optional_identity
        ctx.tf.celltypes.value = 'bytes'
        assert ctx.tf.optional_pins == {'value'}
        ctx.tf.pins.value.set_checksum(Buffer(b'').get_checksum())
        ctx.compute()
        assert ctx.tf.pins.value.checksum == Buffer(None, 'plain').get_checksum()
        assert ctx.tf.result.value is None
        assert 'value' not in ctx.tf().construct().resolve('plain')
