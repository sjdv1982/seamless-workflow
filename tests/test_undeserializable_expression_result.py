"""A bound projection whose result can't be deserialized as its celltype.

Review decisions §3.2 item 7 and §8.4: the expression result stays stored, the
cell is `failed` with a `HashTypeValidationError`, and `clear_exception()`
produces the same failure again.
"""

import pytest

from seamless import Cell
from seamless.checksum.hash_type_validation import HashTypeValidationError


@pytest.mark.parametrize("code,celltype", [("x = (", "python"), ("value: [", "yaml")])
@pytest.mark.xfail(strict=False, reason="contract ahead of code: explicit projection/conversion links and string exceptions")
def test_bound_projection_fails_and_clear_exception_reproduces(make_context, code, celltype):
    ctx = make_context()
    ctx.a = Cell("plain")
    ctx.a.set({"code": code})
    ctx.b = ctx.a.code.as_celltype(celltype)

    for _ in range(2):
        ctx.compute(timeout=10)
        with pytest.raises(HashTypeValidationError):
            _ = ctx.b.value
        assert ctx.b.state == "failed"
        assert isinstance(ctx.b.exception, str) and ctx.b.exception
        ctx.b.clear_exception()
