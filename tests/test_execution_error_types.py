"""Worker failures keep substrate error types (review decisions §5).

The bound runners used to reduce every error to ``WorkflowExecutionError(str(exc))``,
so a ``CacheMissError(checksum)`` lost its type before §8.3 could surface it.
"""

import copy
import sys

import pytest

from seamless import Buffer, CacheMissError, Cell, Checksum
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.checksum.cached_calculate_checksum import checksum_cache
from seamless.checksum import expression as expression_mod
from seamless.checksum.conversion import SeamlessConversionError
from seamless.checksum.expression import ExpressionEvaluationError
from seamless.checksum.hash_type_validation import HashTypeValidationError
from seamless_workflow.errors import WorkflowExecutionError, execution_error


MISSING = Checksum("f" * 64)


def _raised(exc):
    try:
        try:
            raise KeyError("inner")
        except KeyError:
            raise exc
    except Exception as caught:
        return caught


@pytest.mark.parametrize("exc", [
    CacheMissError(MISSING),
    ExpressionEvaluationError("Invalid path segment at 0"),
    HashTypeValidationError("Cannot deserialize checksum as requested celltype"),
    SeamlessConversionError("cannot be converted"),
], ids=lambda exc: type(exc).__name__)
def test_substrate_errors_keep_their_type(exc):
    error = execution_error(_raised(exc))
    assert type(error) is type(exc)
    assert error.args == exc.args
    assert error.__traceback__ is None
    assert error.__context__ is None and error.__cause__ is None
    assert error.failure_id

    copied = copy.deepcopy(error)
    assert type(copied) is type(exc)
    assert copied.args == exc.args
    assert copied.failure_id == error.failure_id


def test_other_errors_become_workflow_execution_error_text():
    error = execution_error(_raised(RuntimeError("boom")))
    assert type(error) is WorkflowExecutionError
    assert str(error) == "boom"
    assert error.failure_id


def test_projection_over_a_missing_buffer_records_cache_miss_error(make_context, monkeypatch):
    # §8.3 will surface this as the cell's exception; until then it is only the fact.
    # No remote is configured: hide seamless_remote, so no database or hashserver is asked.
    monkeypatch.setitem(sys.modules, "seamless_remote", None)
    ctx = make_context(expression_execution="local")
    ctx.a = Cell("plain")
    ctx.a.checksum = MISSING
    ctx.b = ctx.a.x
    ctx.compute(timeout=10)

    errors = [error for _, error in ctx._facts.values() if error is not None]
    assert len(errors) == 1
    assert isinstance(errors[0], CacheMissError)
    assert errors[0].args == (MISSING,)


def _drop_buffer(checksum):
    checksum = Checksum(checksum)
    cache = get_buffer_cache()
    with cache.lock:
        cache.weak_cache.pop(checksum, None)
        cache.strong_cache.pop(checksum, None)
    checksum_cache.pop(checksum, None)
    expression_mod._expression_result_buffers.pop(checksum, None)


def test_bound_cache_miss_failure_can_be_cleared_and_retried(make_context, monkeypatch):
    monkeypatch.setitem(sys.modules, "seamless_remote", None)
    source = Buffer({"x": "available later"}, "plain")
    source_checksum, source_content = source.get_checksum(), source.content
    _drop_buffer(source_checksum)

    ctx = make_context(expression_execution="local")
    ctx.a = Cell("plain")
    ctx.a.checksum = source_checksum
    ctx.b = ctx.a.x
    ctx.compute(timeout=10)

    assert ctx.b.checksum is None
    assert ctx.b.state == "failed"
    assert ctx.b.exception is not None
    assert source_checksum.hex() in str(ctx.b.exception)

    restored = Buffer(source_content, checksum=source_checksum).tempref()
    try:
        ctx.b.clear_exception()
        ctx.compute(timeout=10)
        assert ctx.b.value == "available later"
        assert ctx.b.state == "complete"
        assert ctx.b.exception is None
    finally:
        restored.clear()


def test_bound_missing_result_buffer_keeps_complete_state(make_context, monkeypatch):
    ctx = make_context(expression_execution="local")
    ctx.a = Cell("plain")
    ctx.a.set({"x": "evicted"})
    ctx.b = ctx.a.x
    ctx.compute(timeout=10)
    result = ctx.b.checksum
    assert result is not None
    _drop_buffer(result)

    def no_fingertip(self):
        raise AssertionError("bound Cell.value must not fingertip")

    monkeypatch.setattr(Checksum, "fingertip", no_fingertip)
    with pytest.raises(CacheMissError) as exc_info:
        ctx.b.value
    assert exc_info.value.args == (result,)
    assert ctx.b.state == "complete"
    assert ctx.b.exception is None
