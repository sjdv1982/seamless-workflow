"""A bound Cell recovers its content-addressed join through the public API."""
import pytest
from unittest.mock import AsyncMock
from seamless import Cell, CacheMissError, Checksum
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.checksum.cached_calculate_checksum import checksum_cache
from seamless.checksum import expression
from seamless_remote import buffer_remote, database_remote


def drop(checksum):
    cache = get_buffer_cache()
    with cache.lock:
        cache.weak_cache.pop(checksum, None)
        cache.strong_cache.pop(checksum, None)
    checksum_cache.pop(checksum, None)
    expression._expression_result_buffers.pop(checksum, None)


def test_bound_cell_fingertip_recovers_join_and_upstream_expression(make_context, monkeypatch):
    monkeypatch.setattr(buffer_remote, 'get_buffer', AsyncMock(return_value=None))
    monkeypatch.setattr(database_remote, 'has_read_database', lambda: False)
    ctx = make_context()
    ctx.source = Cell('plain'); ctx.source.set({'nested': {'word': 'workflow-celljoin-recovery'}})
    ctx.member = Cell('plain'); ctx.member = ctx.source['nested']
    ctx.join = Cell('plain'); ctx.join['a'] = ctx.member
    ctx.compute(timeout=10)
    result, member = ctx.join.checksum, ctx.member.checksum
    assert ctx.join.value == {'a': {'word': 'workflow-celljoin-recovery'}}
    drop(result); drop(member)
    with pytest.raises(CacheMissError):
        result.resolve()
    recovered = ctx.join.fingertip()
    assert recovered.get_value('plain') == {'a': {'word': 'workflow-celljoin-recovery'}}
    assert ctx.join.checksum == result
    assert ctx.join.value == {'a': {'word': 'workflow-celljoin-recovery'}}


def test_bound_ordinary_join_missing_member_is_final(make_context, monkeypatch):
    monkeypatch.setattr(buffer_remote, 'get_buffer', AsyncMock(return_value=None))
    monkeypatch.setattr(database_remote, 'has_read_database', lambda: False)
    monkeypatch.setattr(Checksum, 'fingertip', AsyncMock(side_effect=AssertionError('ordinary join must not recover')))
    ctx = make_context()
    missing = Checksum('91' * 32)
    ctx.member = Cell('plain', checksum=missing)
    ctx.join = Cell('plain'); ctx.join['a'] = ctx.member
    ctx.compute(timeout=10)
    assert ctx.join.state == 'failed'
    # Bound Cells expose the failure message, while the core evaluator test
    # checks CacheMissError and its absent fingertip category directly.
    assert missing.hex() in str(ctx.join.exception)
    Checksum.fingertip.assert_not_awaited()
