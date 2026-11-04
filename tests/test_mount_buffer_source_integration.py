"""Write-mounted leaves remain usable inputs after their memory buffers expire."""

import asyncio
import gc
import gzip
import json
import threading
import time
from types import SimpleNamespace

import pytest

from seamless import Buffer, CacheMissError, Cell, Checksum
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.caching.buffer_source import get_buffer_source_lengths
from seamless.checksum.calculate_checksum import calculate_checksum
from seamless.checksum.expression import (
    _result_reachable,
    choose_expression_evaluation_location,
    evaluate_expression_placed,
)
from seamless_workflow import Context
from seamless_workflow.attachments import AttachmentSpec
from seamless_workflow.attachments.fs.service import FileSystemService, get_service


def _wait_for_mount_ack(cell):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = cell.mount.status
        if (status and status.get('in_sync')
                and status.get('node_checksum') == cell.checksum.hex()):
            return
        time.sleep(.01)
    pytest.fail(f'Mount did not acknowledge: {cell.mount.status!r}')


def _evict_leaf(checksum):
    cache = get_buffer_cache()
    with cache.lock:
        entry = cache.strong_cache.get(checksum)
        assert entry is not None and entry.tempref is not None
        entry.tempref.last_refreshed = time.time() - 120
    del entry
    gc.collect()
    cache.run_eviction_once()
    gc.collect()
    assert cache.get(checksum) is None
    assert cache.reference_snapshot().get(checksum, (0, 0, False))[0] == 0


@pytest.fixture(params=[False, True], ids=['raw', 'gzip'])
def mounted_leaf(request, tmp_path, monkeypatch):
    monkeypatch.setattr(FileSystemService, 'poll_interval', 100)
    content = json.dumps({'value': 42, 'unique': str(tmp_path)}).encode()
    checksum = Checksum(calculate_checksum(content))
    leaf = Buffer(content, checksum=checksum)
    leaf.tempref()
    del leaf
    path = tmp_path / 'tree'
    if request.param:
        path.mkdir()
        (path / 'leaf.json.gz').write_bytes(gzip.compress(b'old content'))

    with Context() as context:
        context.source = Cell(celltype='deepfolder')
        context.source.set({'leaf.json': checksum})
        context.backup = context.source.as_celltype('folder')
        context.backup.mount(path, mode='w')
        _wait_for_mount_ack(context.backup)

        _evict_leaf(checksum)
        yield context, checksum, content


def test_evicted_mount_leaf_is_a_local_expression_input(mounted_leaf, monkeypatch):
    _, checksum, _ = mounted_leaf
    read_threads = []
    original_read = FileSystemService._read_bytes

    def track_read(service, path):
        read_threads.append(threading.get_ident())
        return original_read(service, path)

    monkeypatch.setattr(FileSystemService, '_read_bytes', track_read)
    assert choose_expression_evaluation_location(
        checksum, 'value', 'plain', 'int'
    ) == 'local'
    assert not read_threads, 'Placement must not read the whole backing buffer'

    async def evaluate():
        loop_thread = threading.get_ident()
        assert await _result_reachable(checksum)
        result = await evaluate_expression_placed(
            checksum, 'value', 'plain', 'int', execution='auto', scratch=True
        )
        assert (await result.resolution('int')) == 42
        assert read_threads
        assert loop_thread not in read_threads

    asyncio.run(evaluate())


def test_async_mount_fallback_reads_off_the_event_loop(mounted_leaf, monkeypatch):
    _, checksum, content = mounted_leaf
    read_threads = []
    original_read = FileSystemService._read_bytes

    def track_read(service, path):
        read_threads.append(threading.get_ident())
        return original_read(service, path)

    monkeypatch.setattr(FileSystemService, '_read_bytes', track_read)

    async def resolve():
        loop_thread = threading.get_ident()
        buffer = await checksum.resolution()
        assert buffer.content == content
        assert read_threads, 'The fallback must actually read the backing file'
        assert loop_thread not in read_threads

    asyncio.run(resolve())


def test_unmount_removes_fallback_even_when_file_persists(mounted_leaf):
    context, checksum, _ = mounted_leaf
    del context.backup.mount

    async def check():
        assert not await _result_reachable(checksum)
        with pytest.raises(CacheMissError):
            await checksum.resolution()

    asyncio.run(check())


def test_source_lengths_describe_buffer_bytes_when_supplied(mounted_leaf):
    _, checksum, content = mounted_leaf
    length = get_buffer_source_lengths([checksum])[0]
    assert length is None or length == len(content)


def test_rename_can_resolve_a_leaf_from_the_previous_mount_index(mounted_leaf, tmp_path):
    context, checksum, content = mounted_leaf
    context.source.set({'renamed.json': checksum})
    _wait_for_mount_ack(context.backup)
    path = tmp_path / 'tree'
    assert (path / 'renamed.json').read_bytes() == content
    assert not (path / 'leaf.json').exists()
    assert not (path / 'leaf.json.gz').exists()
    gc.collect()
    assert checksum.resolve().content == content


def test_successfully_written_leaf_survives_a_later_delivery_failure(tmp_path):
    content = b'partial delivery leaf ' + str(tmp_path).encode()
    checksum = Checksum(calculate_checksum(content))
    leaf = Buffer(content, checksum=checksum)
    leaf.tempref()
    del leaf
    missing = Checksum(calculate_checksum(b'missing leaf ' + str(tmp_path).encode()))
    path = tmp_path / 'tree'
    service = get_service()
    registration = service.reserve(
        AttachmentSpec(str(path), 'w', 'cell'), 'folder',
        'partial-source-' + str(tmp_path), lambda *args: None,
    )

    def attempt_delivery():
        with pytest.raises(CacheMissError):
            service._write_directory(
                registration, SimpleNamespace(expected_fingerprint=None),
                json.dumps({'a.json': checksum.hex(), 'z.json': missing.hex()}),
            )

    try:
        attempt_delivery()
        assert (path / 'a.json').read_bytes() == content
        _evict_leaf(checksum)
        assert checksum.resolve().content == content
    finally:
        service.unregister(registration).result(timeout=5)
