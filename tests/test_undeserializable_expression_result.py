"""A bound projection whose result can't be deserialized as its celltype.

Review decisions §3.2 item 7 and §8.4: the expression result stays stored, the
cell is `failed` with a `HashTypeValidationError`, and `clear_exception()`
produces the same failure again.
"""

import pytest

from seamless import Cell
from seamless.checksum.hash_type_validation import HashTypeValidationError


@pytest.mark.xfail(
    strict=True,
    reason="§8.3/§8.4 not implemented: the bound cell ends 'blocked' with no exception",
)
@pytest.mark.parametrize("code,celltype", [("x = (", "python"), ("value: [", "yaml")])
def test_bound_projection_fails_and_clear_exception_reproduces(make_context, code, celltype):
    ctx = make_context()
    ctx.a = Cell("plain")
    ctx.a.set({"code": code})
    ctx.b = ctx.a.code
    ctx.b.celltype = celltype

    for _ in range(2):
        ctx.compute(timeout=10)
        assert ctx.b.state == "failed"
        assert isinstance(ctx.b.exception, HashTypeValidationError)
        ctx.b.clear_exception()
