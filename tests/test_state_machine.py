from __future__ import annotations

import pytest

from seamless_workflow import Context
from seamless_workflow.errors import NodeError
from seamless import Checksum


def double(x):
    return 2 * x


def inc(x):
    return x + 1


def fail(x):
    raise RuntimeError("boom")


def test_node_compute_waits_for_upstream_and_releases_result_leases():
    from seamless.caching.buffer_cache import get_buffer_cache
    ctx = Context()
    ctx.double = double
    ctx.inc = inc
    ctx.double.pins.x = 3
    ctx.inc.pins.x = ctx.double

    checksum = ctx.inc.compute(timeout=10)
    assert isinstance(checksum, Checksum)
    assert ctx.double.result.value == 6
    assert ctx.inc.result.value == 7
    baseline = get_buffer_cache().reference_snapshot()[checksum][0]
    assert ctx.inc.compute(timeout=10) == checksum
    assert get_buffer_cache().reference_snapshot()[checksum][0] == baseline
    ctx.close()
    assert get_buffer_cache().reference_snapshot().get(checksum, (0,))[0] == 0


def test_failed_upstream_blocks_downstream_with_error_reason():
    ctx = Context()
    ctx.fail = fail
    ctx.inc = inc
    ctx.fail.pins.x = 1
    ctx.inc.pins.x = ctx.fail
    ctx.compute(timeout=10)

    assert ctx.get_graph(runtime=True)["nodes"][0]["runtime"]["state"] == "failed"
    inc_node = ctx._graph.nodes[("inc",)]
    assert inc_node.state == "blocked"
    assert inc_node.block_reason == "blocked-by-error"
    with pytest.raises(NodeError):
        ctx.inc.compute()


def test_clear_exception_noop_and_successful_rederive_after_edit():
    def reciprocal(x):
        return 1 / x

    ctx = Context()
    ctx.reciprocal = reciprocal
    ctx.reciprocal.pins.x = 0
    ctx.compute(timeout=10)
    assert ctx._graph.nodes[("reciprocal",)].state == "failed"

    ctx.reciprocal.pins.x = 2
    ctx.reciprocal.clear_exception()
    ctx.compute(timeout=10)
    assert ctx.reciprocal.result.value == 0.5


def test_node_state_reports_complete_and_unwired_nodes():
    ctx = Context()
    ctx.value = 10
    ctx.double = double

    assert ctx.value.state == "complete"
    assert ctx.double.state == "unwired"
    assert ctx.double.result.state == "unwired"

    ctx.double.pins.x = ctx.value
    ctx.compute(timeout=10)
    assert ctx.double.state == "complete"
    assert ctx.double.result.state == "complete"


def test_node_state_and_exception_report_own_failure_only():
    ctx = Context()
    ctx.fail = fail
    ctx.inc = inc
    ctx.fail.pins.x = 1
    ctx.inc.pins.x = ctx.fail
    ctx.compute(timeout=10)

    assert ctx.fail.state == "failed"
    assert isinstance(ctx.fail.exception, RuntimeError)
    error = ctx.fail.exception
    assert str(error) == "RuntimeError: boom\n"
    assert ctx.fail.result.state == "failed"
    result_error = ctx.fail.result.exception
    assert result_error.failure_id == error.failure_id
    assert str(result_error) == str(error)
    assert ctx.inc.state == "blocked"
    assert ctx.inc.block_reason == "blocked-by-error"
    assert ctx.inc.exception is None

    ctx.fail.pins.x = 2
    ctx.compute(timeout=10)
    assert str(ctx.fail.exception) == str(error)
    assert ctx.fail.exception.failure_id != error.failure_id


def test_node_state_reports_complete_after_explicit_barrier():
    """The eager flag is removed; explicit barriers observe autonomous work."""

    ctx = Context()
    ctx.double = double
    ctx.double.pins.x = 3
    ctx.compute(timeout=10)

    assert ctx.double.state == "complete"
    assert ctx.double.result.state == "complete"
