"""A bound projection whose result can't be deserialized as its celltype.

Review decisions §3.2 item 7: the expression result stays stored, and only
reading the value fails. contracts/cells.md, `.buffer` and `.value` (ruled
2026-09-28): a failure to materialize a result that exists is raised on every
read and never recorded, so the cell stays `complete`, exactly as the
standalone twin in seamless-core's `test_expression_result_never_undone.py`.
This supersedes the recorded `failed` state of §8.4.
"""

import pytest

from seamless import Cell, Expression
from seamless.checksum.hash_type_validation import HashTypeValidationError


@pytest.mark.parametrize("code,celltype", [("x = (", "python"), ("value: [", "yaml")])
def test_bound_projection_stays_complete_and_every_read_raises(make_context, code, celltype):
    ctx = make_context()
    source = Cell("plain")
    source.set({"code": code})
    # An Expression projects directly into its result celltype. Cell
    # as_celltype() closes the preceding projection into a separate link.
    expression = Expression(
        source, path="code", input_celltype="plain", celltype=celltype,
    )
    expected = expression.compute()
    ctx.b = Cell(source=expression, celltype=celltype)

    ctx.compute(timeout=10)
    assert ctx.b.state == "complete"
    result = ctx.b.checksum
    assert result == expected
    for _ in range(2):
        with pytest.raises(HashTypeValidationError):
            _ = ctx.b.value
        assert ctx.b.state == "complete"
        assert ctx.b.exception is None
        assert ctx.b.checksum == result
    assert ctx.b.buffer is not None
    with pytest.raises(HashTypeValidationError):
        ctx.b.run()
