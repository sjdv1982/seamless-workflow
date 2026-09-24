"""A bound Cell's scratch policy decides how its node holds its result."""
from __future__ import annotations

from seamless.caching.buffer_cache import get_buffer_cache
from seamless_workflow import Context


def _converted_cell(ctx, value):
    ctx.src = value
    ctx.dst = ctx.src
    ctx.dst.celltype = "str"
    ctx.compute(timeout=10)
    return ctx.dst


def test_bound_cell_is_non_scratch_by_default():
    ctx = Context()
    dst = _converted_cell(ctx, 1234567)
    assert dst.scratch is False
    checksum = dst.checksum
    assert checksum is not None
    assert not get_buffer_cache().is_scratch_ref(checksum)


def test_scratch_bound_cell_holds_its_result_as_scratch():
    ctx = Context()
    ctx.src = 7654321
    ctx.dst = ctx.src
    ctx.dst.celltype = "str"
    ctx.dst.scratch = True
    ctx.compute(timeout=10)
    assert ctx.dst.scratch is True
    checksum = ctx.dst.checksum
    assert checksum is not None
    assert get_buffer_cache().is_scratch_ref(checksum)
    assert ctx.get_graph()["nodes"][[n["path"] for n in ctx.get_graph()["nodes"]].index(["dst"])]["scratch"] is True
