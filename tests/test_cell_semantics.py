"""The produced celltype, input recipe, null, and root write contracts."""
import copy
import gc
import re
import pytest
from seamless import Buffer, CacheMissError, Cell, Checksum
from seamless.checksum.hash_type_validation import HashTypeValidationError
from seamless.checksum.null import NULL_CHECKSUM
from seamless.retired_names import RETIRED_NAMES
from seamless_workflow import AuthorityError, PathError


def test_declared_checksum_recipe_survives_binding_and_graph(make_context):
    ctx = make_context()
    buffer = Buffer(5, 'int'); buffer.tempref()
    ctx.a = Cell('str', checksum=buffer.get_checksum(), input_celltype='int')
    ctx.compute(timeout=10)
    assert ctx.a.value == ctx.a.build().run() == '5'
    assert ctx.a.input_celltype == 'int' and ctx.a.celltype == 'str'
    assert ctx.a.source is None
    assert ctx.a._input_ref == buffer.get_checksum()
    graph = ctx.get_graph()
    # Version 0.5 and anonymous nodes are covered independently, so a format
    # gap cannot hide the literal-producer round-trip checks in this test.
    entry, = graph['nodes']
    assert 'target_celltype' not in entry
    assert entry['value']['celltype'] == 'int'
    ctx.set_graph(graph)
    ctx.compute(timeout=10)
    assert ctx.a.input_celltype == 'int' and ctx.a.value == '5'
    ctx.a.set_checksum(buffer.get_checksum(), input_celltype='int')
    assert ctx.a.value == '5'


def test_type_is_copied_once_and_input_type_follows(make_context):
    ctx = make_context()
    ctx.a = Cell('str'); ctx.a.set('42')
    ctx.b = ctx.a
    assert ctx.b.celltype == ctx.b.input_celltype == 'str'
    ctx.a.celltype = 'int'; ctx.compute(timeout=10)
    assert ctx.b.celltype == 'str' and ctx.b.input_celltype == 'int'
    assert ctx.b.value == '42'
    ctx.other = Cell('float'); ctx.other.set(7)
    ctx.b = ctx.other; ctx.compute(timeout=10)
    assert ctx.b.celltype == 'str' and ctx.b.input_celltype == 'float'
    assert ctx.b.value == '7.0'
    with pytest.raises(AttributeError): ctx.b.input_celltype = 'text'
    with pytest.raises(AttributeError, match="'target_celltype' has been retired; use celltype instead"):
        ctx.b.target_celltype = 'int'


def test_source_reports_nearest_configured_edge(make_context):
    ctx = make_context()
    ctx.source = {'b': {'x': 3}}
    ctx.connected = ctx.source
    ctx.literal = {'b': {'x': 1}, 'c': 2}
    ctx.branch = {'x': 9}
    ctx.literal.b = ctx.branch
    assert ctx.literal.source is None
    source = ctx.source._workflow_endpoint()
    assert ctx.connected.source._workflow_endpoint() == source
    assert ctx.connected.b.x.source._workflow_endpoint() == source
    assert ctx.literal.b.source._workflow_endpoint() == ctx.branch._workflow_endpoint()
    assert ctx.literal.b.x.source._workflow_endpoint() == ctx.branch._workflow_endpoint()
    assert ctx.literal.c.source is None
    assert ctx.literal.checksum is not None


@pytest.mark.parametrize('form', ['value', 'buffer', 'checksum'])
def test_root_write_matrix_and_detach(make_context, form):
    ctx = make_context()
    ctx.a = Cell('int'); ctx.a.set(1)
    ctx.b = ctx.a
    buffer = Buffer(7, 'int'); buffer.tempref()
    value = {'value': 7, 'buffer': buffer, 'checksum': buffer.get_checksum()}[form]
    write = {'value': ctx.b.set, 'buffer': ctx.b.set_buffer, 'checksum': ctx.b.set_checksum}[form]
    with pytest.raises(AuthorityError): write(value)
    setattr(ctx.b, form, value)
    ctx.compute(timeout=10)
    assert ctx.b.source is None
    assert ctx.b.checksum == buffer.get_checksum() and ctx.b.value == 7
    write(value)
    assert ctx.b.checksum == buffer.get_checksum()
    assert ctx.a.value == 1


def test_clear_blocks_downstream_and_null_flows(make_context):
    ctx = make_context()
    ctx.a = Cell('int'); ctx.a.set(4)
    ctx.b = ctx.a; ctx.tail = ctx.b
    ctx.b.checksum = None; ctx.compute(timeout=10)
    assert ctx.b.state == 'unwired' and ctx.b.source is None
    assert ctx.b.input_celltype is None and ctx.tail.state == 'blocked'
    ctx.b.value = None; ctx.compute(timeout=10)
    assert ctx.b.checksum == Checksum(NULL_CHECKSUM)
    assert ctx.b.value is None and ctx.tail.value is None
    ctx.b.celltype = 'binary'; ctx.compute(timeout=10)
    assert ctx.b.checksum == Checksum(NULL_CHECKSUM)
    ctx.b.buffer = None
    assert ctx.b.state == 'unwired'


def test_failed_own_conversion_reports_exception(make_context):
    ctx = make_context()
    ctx.a = Cell('str'); ctx.a.set('hello')
    ctx.a.celltype = 'int'; ctx.compute(timeout=10)
    assert ctx.a.state == 'failed' and ctx.a.exception is not None
    assert ctx.a.checksum is None
    ctx.a.celltype = 'text'; ctx.compute(timeout=10)
    assert ctx.a.value == ctx.a.build().run() == 'hello'


@pytest.mark.parametrize('form', ['buffer', 'set_buffer'])
def test_buffer_validation_is_eager_checksum_validation_is_deferred(make_context, form):
    ctx = make_context()
    ctx.a = Cell('int'); ctx.a.set(3)
    invalid = Buffer(b'not an int')
    with pytest.raises(HashTypeValidationError):
        if form == 'buffer': ctx.a.buffer = invalid
        else: ctx.a.set_buffer(invalid)
    assert ctx.a.value == 3
    ctx.a.checksum = invalid.get_checksum()
    # A checksum declaration neither resolves nor validates its bytes.
    assert ctx.a.checksum == invalid.get_checksum()


@pytest.mark.parametrize('version', ['0.2', '0.3'])
def test_legacy_graph_requires_matching_types(make_context, version):
    ctx = make_context(); ctx.a = Cell('int'); ctx.a.set(1)
    graph = ctx.get_graph(); graph['__seamless_workflow__'] = version
    graph['nodes'][0]['target_celltype'] = 'int'
    ctx.set_graph(graph); assert ctx.a.value == 1
    graph['nodes'][0]['target_celltype'] = 'str'
    with pytest.raises(PathError): ctx.set_graph(graph)


@pytest.mark.parametrize('celltype', ['int', 'plain'])
@pytest.mark.parametrize('disk', ['missing', 'empty', 'null'])
def test_null_mount_preserves_file_representation(make_context, tmp_path, celltype, disk):
    path = tmp_path / 'value'
    if disk != 'missing': path.write_bytes(b'' if disk == 'empty' else b'null\n')
    before = path.stat().st_mtime_ns if path.exists() else None
    ctx = make_context(); ctx.a = Cell(celltype); ctx.a.mount(path)
    ctx.mounts.sync(timeout=10)
    assert ctx.a.value is None and ctx.a.checksum == Checksum(NULL_CHECKSUM)
    if disk == 'missing': assert not path.exists()
    else:
        assert path.read_bytes() == (b'' if disk == 'empty' else b'null\n')
        assert path.stat().st_mtime_ns == before
    with pytest.raises(AuthorityError): ctx.a.checksum = None
    with pytest.raises(AuthorityError): ctx.a.buffer = None
    ctx.a.set(8); ctx.mounts.sync(timeout=10)
    assert path.read_bytes() == b'8\n'
    ctx.a.set(None); ctx.mounts.sync(timeout=10)
    assert path.exists() and path.read_bytes() == b''


def test_connected_retype_checksum_value_downstream_and_mount_agree(make_context, tmp_path):
    ctx = make_context(); ctx.a = Cell('str'); ctx.a.set('hello')
    ctx.b = Cell('text'); ctx.b = ctx.a; ctx.tail = ctx.b
    ctx.compute(timeout=10)
    path = tmp_path / 'output'
    ctx.b.mount(path, 'w'); ctx.mounts.sync(timeout=10)
    assert ctx.b.checksum == Buffer('hello', 'text').get_checksum()
    assert ctx.b.value == ctx.tail.value == ctx.b.build().run() == 'hello'
    assert path.read_bytes() == b'hello\n'


@pytest.mark.parametrize('celltype', ['folder', 'deepfolder'])
def test_directory_missing_is_null_but_empty_is_mapping(make_context, tmp_path, celltype):
    path = tmp_path / 'directory'
    ctx = make_context(); ctx.a = Cell(celltype); ctx.a.mount(path, mode='r')
    assert ctx.a.value is None and not path.exists()
    path.mkdir(); ctx.mounts.sync(timeout=10)
    assert ctx.a.value == {} and ctx.a.checksum != Checksum(NULL_CHECKSUM)


def test_bytes_value_and_run_agree(make_context):
    ctx = make_context(); ctx.a = Cell('bytes'); ctx.a.set(b'payload')
    assert ctx.a.value == ctx.a.run() == ctx.a.build().run() == b'payload'


@pytest.mark.parametrize('suffix', ['.gz', '.zst'])
def test_compressed_mount_null_is_an_empty_file(make_context, tmp_path, suffix):
    if suffix == '.zst': pytest.importorskip('zstandard')
    path = tmp_path / ('value' + suffix)
    path.write_bytes(b'')
    ctx = make_context(); ctx.a = Cell('int'); ctx.a.mount(path)
    assert ctx.a.value is None
    ctx.a.set(12); ctx.mounts.sync(timeout=10)
    assert path.read_bytes()
    ctx.a.set(None); ctx.mounts.sync(timeout=10)
    assert path.read_bytes() == b''


def _identity(value):
    return value


def test_source_assignment_replaces_transformer_and_copies_type(make_context):
    ctx = make_context(); ctx.source = Cell('int'); ctx.source.set(12)
    ctx.target = _identity; ctx.target.pins.value = 3
    ctx.tail = ctx.target
    ctx.compute(timeout=10)
    ctx.target = ctx.source; ctx.compute(timeout=10)
    assert isinstance(ctx.target, Cell)
    assert ctx.target.celltype == ctx.target.input_celltype == 'int'
    assert ctx.target.value == ctx.tail.value == 12
    ctx.source.set(13); ctx.compute(timeout=10)
    assert ctx.target.value == ctx.tail.value == 13


@pytest.mark.parametrize('form', ['buffer', 'set_buffer'])
def test_buffer_writes_deposit_the_buffer(make_context, form):
    # Without a tempref, a dropped buffer that nothing deposited can't be resolved.
    control = Buffer(f'undeposited bound {form}', 'text'); checksum = control.get_checksum()
    del control; gc.collect()
    with pytest.raises(CacheMissError): checksum.resolve('text')
    ctx = make_context()
    ctx.a = Cell('text')
    ctx.tf = _identity; ctx.tf.celltypes.value = 'text'
    for handle, content in ((ctx.a, f'deposited bound cell {form}'),
                            (ctx.tf.pins.value, f'deposited bound pin {form}')):
        buffer = Buffer(content, 'text')
        if form == 'buffer': handle.buffer = buffer
        else: handle.set_buffer(buffer)
        del buffer; gc.collect()
    ctx.compute(timeout=10)
    assert ctx.a.value == f'deposited bound cell {form}'
    assert ctx.tf.pins.value.value == f'deposited bound pin {form}'


def test_source_and_checksum_of_connected_cells(make_context):
    ctx = make_context()
    # Upstream not complete: unwired, and failed.
    ctx.a = Cell('int'); ctx.b = ctx.a
    ctx.f = Cell('str'); ctx.f.set('hello'); ctx.g = ctx.f; ctx.f.celltype = 'int'
    # The connected cell's own conversion fails.
    ctx.s = Cell('str'); ctx.s.set('hello'); ctx.t = ctx.s; ctx.tail = ctx.t; ctx.t.celltype = 'int'
    # A conversion that keeps the bytes.
    ctx.i = Cell('int'); ctx.i.set(42); ctx.j = ctx.i; ctx.j.celltype = 'float'
    ctx.compute(timeout=10)
    for target, source in ((ctx.b, ctx.a), (ctx.g, ctx.f), (ctx.t, ctx.s), (ctx.j, ctx.i)):
        assert target.source._workflow_endpoint() == source._workflow_endpoint()
    assert ctx.b.checksum is None
    assert (ctx.b.state, ctx.b.block_reason) == ('blocked', 'blocked-by-unwired')
    assert ctx.f.state == 'failed' and ctx.g.checksum is None
    assert (ctx.g.state, ctx.g.block_reason) == ('blocked', 'blocked-by-error')
    assert ctx.t.state == 'failed' and ctx.t.checksum is None
    assert (ctx.tail.state, ctx.tail.block_reason) == ('blocked', 'blocked-by-error')
    assert ctx.j.checksum == ctx.i.checksum and ctx.j.value == 42.0


@pytest.mark.parametrize('name,replacement', sorted(RETIRED_NAMES.items()))
def test_retired_names_are_guarded_on_bound_handles(make_context, name, replacement):
    ctx = make_context()
    ctx.a = {'x': 1}; ctx.tf = _identity
    message = re.escape(f"'{name}' has been retired; use {replacement} instead")
    for handle in (ctx.a, ctx.a.x, ctx.tf.pins.value):
        with pytest.raises(AttributeError, match=message): getattr(handle, name)
        with pytest.raises(AttributeError, match=message): setattr(handle, name, 1)
        with pytest.raises(AttributeError, match=message): delattr(handle, name)


def _check_reads(cell, expected, checksum):
    assert cell.value == cell.run() == cell.build().run() == expected
    assert type(cell.value) is type(expected)
    assert cell.checksum == checksum


def test_standalone_and_bound_retype_agree(make_context):
    ctx = make_context()
    standalone = Cell('int'); standalone.set(5)
    ctx.retyped = standalone
    standalone.celltype = 'float'; ctx.retyped.celltype = 'float'
    ctx.compute(timeout=10)
    for cell in (standalone, ctx.retyped):
        _check_reads(cell, 5.0, Buffer(5, 'int').get_checksum())
        assert cell.input_celltype == 'int'


def test_typed_source_constructions_agree(make_context):
    ctx = make_context()
    upstream = Cell('str'); upstream.set('5')
    ctx.upstream = upstream
    ctx.connected = Cell('text', source=ctx.upstream)
    ctx.rewired = ctx.upstream; ctx.rewired.celltype = 'text'
    ctx.downstream = ctx.connected
    ctx.compute(timeout=10)
    checksum = Buffer('5', 'text').get_checksum()
    expression_cell = Cell('text', source=upstream.build())
    assert expression_cell.checksum is None
    assert expression_cell.state == 'waiting'
    expression_cell.compute()
    for cell in (Cell('text', source=upstream), expression_cell, ctx.connected,
                 ctx.rewired, ctx.downstream):
        _check_reads(cell, '5', checksum)


def test_set_takes_values_only_on_bound_handles(make_context):
    ctx = make_context()
    ctx.source = Cell('int'); ctx.source.set(1)
    ctx.a = {'x': 1}; ctx.tf = _identity
    ctx.compute(timeout=10)
    references = ((ctx.source, r'Cell\(source=\.\.\.\)'),
                  (ctx.source.checksum, r'use \.set_checksum\(\)'))
    for handle in (ctx.a, ctx.a.x, ctx.tf.pins.value):
        for reference, message in references:
            with pytest.raises(TypeError, match=message): handle.set(reference)
            with pytest.raises(TypeError, match=message): handle.value = reference
    ctx.compute(timeout=10)
    assert ctx.get_graph()['connections'] == []
    assert ctx.a.value == {'x': 1}


@pytest.mark.parametrize('edge,blocked', [('root', True), ('b', True), ('z', False)])
def test_projection_set_is_blocked_by_an_edge_on_its_path(make_context, edge, blocked):
    ctx = make_context()
    ctx.source = {'c': 0}
    ctx.a = {'b': {'c': 1}, 'z': {'c': 2}}
    if edge == 'root': ctx.a = ctx.source
    else: setattr(ctx.a, edge, ctx.source)
    ctx.compute(timeout=10)
    if blocked:
        with pytest.raises(AuthorityError): ctx.a.b.c.set(12)
        return
    ctx.a.b.c.set(12); ctx.compute(timeout=10)
    assert ctx.a.value == {'b': {'c': 12}, 'z': {'c': 0}}
    assert ctx.a.z.source._workflow_endpoint() == ctx.source._workflow_endpoint()


def test_checksum_is_a_value_for_bound_checksum_cells_and_pins(make_context):
    ctx = make_context()
    pointer = Checksum('ab' * 32)
    ctx.a = Cell('checksum'); ctx.a = pointer
    ctx.b = Cell('checksum'); ctx.b.set(pointer)
    ctx.c = Cell('checksum'); ctx.c.value = pointer
    ctx.tf = _identity; ctx.tf.celltypes.value = 'checksum'
    ctx.tf.pins.value = pointer
    ctx.compute(timeout=10)
    for handle in (ctx.a, ctx.b, ctx.c, ctx.tf.pins.value):
        assert isinstance(handle.value, Checksum) and handle.value == pointer
        assert handle.checksum == Buffer(pointer, 'checksum').get_checksum()
    ctx.n = Cell('int')
    with pytest.raises(TypeError, match=r'use \.set_checksum\(\)'): ctx.n.set(pointer)
