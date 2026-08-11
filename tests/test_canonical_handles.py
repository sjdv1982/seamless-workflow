from __future__ import annotations

import pytest

from seamless import Cell
from seamless_transformer import direct
from seamless_transformer.transformation_class import Transformation
from seamless_workflow import Context
from seamless_workflow.endpoints import BoundEndpoint
from seamless_workflow.errors import PathError, ReadOnlyEndpointError, StaleWorkflowHandleError


def add(x, y):
    return x + y


def test_context_lookups_are_canonical_handles_and_projection_is_uniform():
    ctx = Context()
    ctx.data = {"value": {"n": 2}, "pins": 4}

    assert isinstance(ctx.data, Cell)
    assert ctx.data["value"].n.value == 2
    assert ctx.data.pins.value == 4
    assert isinstance(ctx.data._workflow_endpoint(), BoundEndpoint)
    assert ctx.data._workflow_endpoint().local_path == ()


def test_bound_projection_value_update_and_deep_source_connection():
    ctx = Context()
    ctx.data = {"nested": {"n": 1}}
    ctx.data.nested.n.set(3)
    assert ctx.data.value == {"nested": {"n": 3}}

    ctx.out = ctx.data.nested.n
    assert ctx.out.value == 3
    assert ctx.get_graph()["connections"][0]["source"] == ["data", "nested", "n"]
    with pytest.raises(PathError):
        ctx.data.nested.n = ctx.out


def test_transformer_result_is_read_only_and_delayed_calls_are_snapshots():
    ctx = Context()
    ctx.add = add
    ctx.add.pins.x = 2
    ctx.add.pins.y = 5
    assert isinstance(ctx.add.result, Cell)
    assert ctx.add.result.value == 7
    with pytest.raises(ReadOnlyEndpointError):
        ctx.add.result.set(10)
    assert isinstance(ctx.add(), Transformation)


def test_direct_handle_is_canonical_and_detached():
    ctx = Context()
    ctx.add = direct(add)
    ctx.add.pins.x = 2
    ctx.add.pins.y = 5
    assert ctx.add() == 7


def test_stale_handles_raise_typed_error():
    ctx = Context()
    ctx.data = 1
    handle = ctx.data
    del ctx.data
    with pytest.raises(StaleWorkflowHandleError):
        _ = handle.value
