"""Cell contracts previously supported only by source inspection."""

import pytest

from seamless import Cell


def test_join_observation_log_contains_no_transformations(
    make_context, transformation_observations,
):
    ctx = make_context()
    ctx.left = 12
    ctx.right = "first"
    ctx.join = Cell("plain")
    ctx.join.set({"kept": True})
    ctx.join.left = ctx.left
    ctx.join.right = ctx.right
    ctx.compute(timeout=10)
    first_checksum = ctx.join.checksum
    assert ctx.join.value == {"kept": True, "left": 12, "right": "first"}
    assert transformation_observations.entries() == []

    ctx.right = "second"
    ctx.compute(timeout=10)
    assert ctx.join.value == {"kept": True, "left": 12, "right": "second"}
    assert ctx.join.checksum != first_checksum
    assert transformation_observations.entries() == []

    ctx.right = "first"
    ctx.compute(timeout=10)
    assert ctx.join.checksum == first_checksum
    assert transformation_observations.entries() == []


@pytest.mark.xfail(
    strict=False,
    reason="contract ahead of code: Cell.exception still returns exception objects, not strings",
)
def test_bound_exception_reports_projection_failure_during_derivation(make_context):
    ctx = make_context()
    ctx.source = Cell("plain")
    ctx.source.set({"present": 7})
    ctx.target = Cell("plain")
    # A missing source subpath fails while deriving the connected target;
    # neither endpoint requests a celltype conversion.
    ctx.target = ctx.source["missing"]
    ctx.compute(timeout=10)

    assert ctx.source.state == "complete"
    assert ctx.source.exception is None
    assert ctx.target.state == "failed"
    assert ctx.target.checksum is None
    error = ctx.target.exception
    assert isinstance(error, str) and error
    assert ctx.target.exception == error

    ctx.source.set({"present": 7, "missing": 11})
    ctx.compute(timeout=10)
    assert ctx.target.state == "complete"
    assert ctx.target.value == 11
    assert ctx.target.exception is None
