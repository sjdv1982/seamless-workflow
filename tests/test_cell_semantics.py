"""The produced celltype, input recipe, null, and root write contracts."""
import copy
import pytest
from seamless import Buffer, Cell, Checksum
from seamless.checksum.null import NULL_CHECKSUM
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
    assert graph['__seamless_workflow__'] == '0.4'
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
    with pytest.raises(AttributeError, match='celltype'): ctx.b.target_celltype = 'int'


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


def test_buffer_validation_is_eager_checksum_validation_is_deferred(make_context):
    ctx = make_context()
    ctx.a = Cell('int'); ctx.a.set(3)
    invalid = Buffer(b'not an int')
    with pytest.raises(Exception): ctx.a.buffer = invalid
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
    ctx = make_context(); ctx.a = Cell(celltype); ctx.a.mount(path)
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
