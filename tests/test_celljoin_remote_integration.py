"""Context joins exercise real placement against in-memory remote services."""
import pytest
from seamless import Buffer, Cell, Checksum
from seamless.caching import buffer_writer
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.checksum.cached_calculate_checksum import checksum_cache
from seamless.checksum import celljoin as joins, expression
from seamless_remote import buffer_remote, database_remote, jobserver_remote


def drop(checksum):
    checksum = Checksum(checksum)
    cache = get_buffer_cache()
    with cache.lock:
        cache.weak_cache.pop(checksum, None)
        cache.strong_cache.pop(checksum, None)
    checksum_cache.pop(checksum, None)
    expression._expression_result_buffers.pop(checksum, None)


@pytest.fixture
def services(monkeypatch):
    """Fake service boundaries; execute recipes using production evaluators."""
    buffer_writer.flush()
    buffers, calls, expression_rows, join_rows = {}, [], {}, {}
    async def get(checksum):
        checksum = Checksum(checksum)
        calls.append(('get', checksum))
        payload = buffers.get(checksum)
        return None if payload is None else Buffer(payload, checksum=checksum)
    async def write(checksum, buffer):
        checksum = Checksum(checksum)
        buffers[checksum] = buffer.content
        calls.append(('write', checksum))
        return True
    async def has(checksums):
        calls.append(('has', tuple(checksums)))
        return [Checksum(checksum) in buffers for checksum in checksums]
    async def lengths(checksums):
        return [len(buffers[Checksum(c)]) if Checksum(c) in buffers else None for c in checksums]
    async def get_expression(checksum, path, source, target):
        return expression_rows.get((Checksum(checksum), path, source, target))
    async def set_expression(checksum, path, source, target, result):
        expression_rows[(Checksum(checksum), path, source, target)] = Checksum(result)
        return True
    async def get_join(checksum, celltype):
        return join_rows.get((Checksum(checksum), celltype))
    async def set_join(checksum, celltype, result):
        join_rows[(Checksum(checksum), celltype)] = Checksum(result)
        return True
    async def get_hash_type(checksum):
        return None
    async def set_hash_type(*args):
        return True
    async def run_expression(checksum, path, source, target, *, scratch):
        checksum = Checksum(checksum)
        calls.append(('expression', checksum, path, source, target, scratch))
        source_buffer = Buffer(buffers[checksum], checksum=checksum)
        key = expression.ExpressionKey(checksum, path, source, target)
        result = expression._evaluate_expression_after_validation(
            key, source_buffer, expression.parse_path(path), expression._cache_key(key),
            lambda: source_buffer)
        produced = result.resolve()
        if not scratch:
            await write(result, produced)
        # The executing process's private buffers cannot answer local placement.
        drop(checksum); drop(result)
        return result
    async def run_join(checksum, celltype, *, scratch):
        checksum = Checksum(checksum)
        calls.append(('celljoin', checksum, celltype, scratch))
        spec = joins.parse_celljoin(Buffer(buffers[checksum], checksum=checksum), celltype)
        produced = joins.evaluate_celljoin(spec, lambda c: Buffer(buffers[Checksum(c)], checksum=Checksum(c)))
        result = produced.get_checksum()
        if not scratch:
            await write(result, produced)
        drop(result)
        return result
    monkeypatch.setattr(buffer_remote, 'get_buffer', get)
    monkeypatch.setattr(buffer_remote, 'write_buffer', write)
    monkeypatch.setattr(buffer_remote, 'has_buffers', has)
    monkeypatch.setattr(buffer_remote, 'get_buffer_lengths', lengths)
    monkeypatch.setattr(buffer_remote, 'has_write_server', lambda: True)
    monkeypatch.setattr(buffer_remote, 'has_read_server', lambda: True)
    monkeypatch.setattr(buffer_remote, '_read_folders_clients', [])
    monkeypatch.setattr(database_remote, 'get_expression_result', get_expression)
    monkeypatch.setattr(database_remote, 'set_expression_result', set_expression)
    monkeypatch.setattr(database_remote, 'get_celljoin_result', get_join)
    monkeypatch.setattr(database_remote, 'set_celljoin_result', set_join)
    monkeypatch.setattr(database_remote, 'get_hash_type', get_hash_type)
    monkeypatch.setattr(database_remote, 'set_hash_type', set_hash_type)
    monkeypatch.setattr(database_remote, 'has_write_server', lambda: True)
    monkeypatch.setattr(database_remote, 'has_read_database', lambda: False)
    monkeypatch.setattr(jobserver_remote, 'has_jobserver', lambda: True)
    monkeypatch.setattr(jobserver_remote, 'run_expression', run_expression)
    monkeypatch.setattr(jobserver_remote, 'run_celljoin', run_join)
    expression.get_expression_cache().clear()
    def server_only(value, celltype):
        buffer = Buffer(value, celltype)
        checksum = buffer.get_checksum()
        buffers[checksum] = buffer.content
        drop(checksum)
        return checksum
    yield server_only, buffers, calls
    buffer_writer.flush()


@pytest.mark.parametrize('scratch', [False, True])
@pytest.mark.parametrize('kind', ['member', 'root'])
def test_server_only_buffer_producing_conversion_completes_locally(make_context, services, scratch, kind):
    server_only, buffers, calls = services
    ctx = make_context()
    if kind == 'member':
        source = server_only('73189', 'text')
        ctx.source = Cell('text', checksum=source)
        ctx.join = Cell('plain'); ctx.join.scratch = scratch
        ctx.join['a'] = ctx.source
        expected = {'a': 73189}
    else:
        source = server_only('{"kept": 91837}', 'text')
        ctx.join = Cell('plain'); ctx.join.scratch = scratch
        ctx.member = Cell('plain'); ctx.member.set(73189)
        ctx.join['a'] = ctx.member
        # Graph loading installs the literal root and incoming member edge
        # atomically, without an ordinary root-only conversion in between.
        graph = ctx.get_graph()
        join_node = next(node for node in graph['nodes'] if node['path'] == ['join'])
        join_node['value'] = {'checksum': source.hex(), 'celltype': 'text'}
        ctx = make_context(); ctx.set_graph(graph)
        expected = {'kept': 91837, 'a': 73189}
    ctx.compute(timeout=10)
    assert ctx.join.state == 'complete', ctx.join.exception
    assert ctx.join.value == expected
    assert ('get', source) in calls
    assert not [call for call in calls if call[0] == 'expression']


@pytest.mark.parametrize('scratch', [False, True])
def test_server_only_checksum_preserving_member_dispatches_celljoin(make_context, services, scratch):
    server_only, buffers, calls = services
    source = server_only({'a': 1}, 'plain')
    ctx = make_context()
    ctx.source = Cell('plain', checksum=source)
    ctx.join = Cell('mixed'); ctx.join.scratch = scratch
    ctx.join['a'] = ctx.source
    ctx.compute(timeout=10)
    assert ctx.join.state == 'complete', ctx.join.exception
    expected = Buffer({'a': {'a': 1}}, 'mixed').get_checksum()
    assert ctx.join.checksum == expected
    dispatched = [call for call in calls if call[0] == 'celljoin']
    assert dispatched and all(call[2:] == ('mixed', scratch) for call in dispatched)
    definition = dispatched[-1][1]
    spec = joins.parse_celljoin(Buffer(buffers[definition], checksum=definition), 'mixed')
    assert dict(spec.members)['a'] == source
    assert not [call for call in calls if call[0] == 'expression']
    if not scratch:
        assert expected in buffers
        assert ctx.join.value == {'a': {'a': 1}}


@pytest.mark.parametrize('scratch', [False, True])
def test_server_only_projection_dispatches_non_scratch_expression_before_join(make_context, services, scratch):
    server_only, buffers, calls = services
    source = server_only({'nested': {'word': 'published-projection'}}, 'plain')
    ctx = make_context()
    ctx.source = Cell('plain', checksum=source)
    ctx.join = Cell('plain'); ctx.join.scratch = scratch
    ctx.join['a'] = ctx.source['nested']
    ctx.compute(timeout=10)
    assert ctx.join.state == 'complete', ctx.join.exception
    expected = Buffer({'a': {'word': 'published-projection'}}, 'plain').get_checksum()
    assert ctx.join.checksum == expected
    if not scratch:
        assert ctx.join.value == {'a': {'word': 'published-projection'}}
    dispatches = [call for call in calls if call[0] == 'expression' and call[1] == source and call[2]]
    assert dispatches and all(call[-1] is False for call in dispatches)
    child = Buffer({'word': 'published-projection'}, 'plain').get_checksum()
    assert child in buffers
    assert ('write', child) in calls
    assert calls.index(dispatches[0]) < calls.index(('write', child))
