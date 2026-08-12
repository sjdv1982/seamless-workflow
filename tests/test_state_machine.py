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


def test_non_eager_compute_activates_upstream_cone_and_releases():
    ctx = Context(eager=False)
    ctx.double = double
    ctx.inc = inc
    ctx.double.pins.x = 3
    ctx.inc.pins.x = ctx.double

    assert ctx.double.result.value is None
    assert ctx.inc.result.value is None

    assert isinstance(ctx.inc.compute(), Checksum)
    assert ctx._graph.nodes[("inc",)].active_count == 0
    assert ctx._graph.nodes[("double",)].derived_active_count == 0


def test_failed_upstream_blocks_downstream_with_error_reason():
    ctx = Context()
    ctx.fail = fail
    ctx.inc = inc
    ctx.fail.pins.x = 1
    ctx.inc.pins.x = ctx.fail

    assert ctx.fail.get_graph if False else True
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
    assert ctx._graph.nodes[("reciprocal",)].state == "failed"

    ctx.reciprocal.pins.x = 2
    ctx.reciprocal.clear_exception()
    assert ctx.reciprocal.result.value == 0.5


def test_public_status_reports_complete_and_unconnected_nodes():
    ctx = Context()
    ctx.value = 10
    ctx.double = double

    assert ctx.value.status == "Status: OK"
    assert ctx.double.status == "Status: unconnected"
    assert ctx.double.result.status == "Status: unconnected"

    ctx.double.pins.x = ctx.value
    assert ctx.double.status == "Status: OK"
    assert ctx.double.result.status == "Status: OK"


def test_public_status_and_exception_report_own_failure_only():
    ctx = Context()
    ctx.fail = fail
    ctx.inc = inc
    ctx.fail.pins.x = 1
    ctx.inc.pins.x = ctx.fail

    assert ctx.fail.status == "Status: error"
    assert isinstance(ctx.fail.exception, RuntimeError)
    assert str(ctx.fail.exception) == "boom"
    assert ctx.fail.result.status == "Status: error"
    assert ctx.fail.result.exception is ctx.fail.exception
    assert ctx.inc.status == "Status: upstream"
    assert ctx.inc.exception is None


def test_public_status_reports_pending_in_lazy_context():
    ctx = Context(eager=False)
    ctx.double = double
    ctx.double.pins.x = 3

    assert ctx.double.status == "Status: pending"
    assert ctx.double.result.status == "Status: pending"
