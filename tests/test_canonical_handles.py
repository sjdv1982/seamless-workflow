from __future__ import annotations

import asyncio

import pytest

from seamless import Cell
from seamless import Checksum
from seamless import Buffer
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


def test_bound_transformer_assignment_wires_result_instead_of_rebinding():
    ctx = Context()
    ctx.add = add
    ctx.add.pins.x = 2
    ctx.add.pins.y = 5
    ctx.out = ctx.add

    assert isinstance(ctx.out, Cell)
    assert ctx.out.value == 7
    assert ctx.add.result.value == 7


def test_original_bound_alias_and_fresh_lookup_share_state():
    source = Cell({"value": 1})
    ctx = Context()
    ctx.source = source
    assert source.value == ctx.source.value == {"value": 1}
    ctx.source["value"] = 4
    assert source.value == ctx.source.value == {"value": 4}

    tf = direct(add)
    ctx.tf = tf
    tf.pins.x = 3
    ctx.tf.pins.y = 4
    assert tf.pins.y == 4
    assert ctx.tf() == 7


def test_rmw_preserves_unrelated_one_level_edges_and_augmented_writes_once():
    ctx = Context()
    ctx.left = 10
    ctx.right = 20
    ctx.data = {"left": 0, "right": 0, "nested": {"n": 1}}
    ctx.data.left = ctx.left
    ctx.data.right = ctx.right
    ctx.data.nested.n += 1

    assert ctx.data.value == {"left": 10, "right": 20, "nested": {"n": 2}}
    targets = {tuple(edge["target"]) for edge in ctx.get_graph()["connections"]}
    assert ("data", "left") in targets
    assert ("data", "right") in targets


def test_connection_target_depth_and_sequence_validation():
    ctx = Context()
    ctx.src = 3
    ctx.data = [0]
    ctx.data[0] = ctx.src
    assert ctx.data.value == [3]

    with pytest.raises(PathError):
        ctx.data[0].child = ctx.src

    ctx.mapping = {}
    ctx.mapping["x"] = ctx.src
    assert ctx.mapping.value == {"x": 3}

    ctx.sequence = {}
    with pytest.raises(TypeError):
        ctx.sequence[0] = ctx.src

    with pytest.raises(PathError):
        ctx.mapping[0:1] = ctx.src


def test_bound_cell_demand_has_snapshot_and_reactive_return_types():
    ctx = Context()
    ctx.data = {"n": 4}
    assert ctx.data.compute() == ctx.data.checksum
    assert ctx.data.run() == {"n": 4}

    ctx.add = add
    ctx.add.pins.x = 2
    ctx.add.pins.y = 3
    snapshot = ctx.add()
    ctx.add.pins.x = 10
    assert snapshot.run() == 5
    assert ctx.add.run() == 13
    assert isinstance(ctx.add.compute(), Checksum)
    assert asyncio.run(ctx.add.task()) == 13


def test_non_eager_demand_releases_activation_leases():
    ctx = Context(eager=False)
    ctx.add = add
    ctx.add.pins.x = 2
    ctx.add.pins.y = 3
    assert ctx._graph.nodes[("add",)].state == "waiting"
    assert isinstance(ctx.add.compute(), Checksum)
    assert ctx._graph.nodes[("add",)].active_count == 0
    assert ctx._graph.nodes[("add",)].derived_active_count == 0


def test_transformer_pin_namespaces_collisions_and_metadata_parity():
    def pins(scratch, inp):
        return (scratch, inp)

    ctx = Context()
    ctx.tf = pins
    ctx.tf["scratch"] = "pin"
    ctx.tf.pins.inp = "input"
    assert ctx.tf.scratch is False
    assert ctx.tf["scratch"] == "pin"
    assert ctx.tf.inp == "input"
    assert ctx.tf.pins.inp == ctx.tf.args.inp == "input"
    with pytest.raises(AttributeError):
        _ = ctx.tf.typo

    metadata = ctx.tf.meta
    metadata["changed"] = True
    assert "changed" not in ctx.tf.meta
    ctx.tf.meta = {"changed": True}
    assert ctx.tf.meta["changed"] is True
    assert isinstance(ctx.tf.code, Buffer)

    del ctx.tf["scratch"]
    assert ctx.tf["scratch"] is None


def test_graph_roundtrip_preserves_call_mode_and_source_paths():
    ctx = Context()
    ctx.data = {"nested": {"value": 2}}
    ctx.add = add
    ctx.add.pins.x = 3
    ctx.add.pins.y = 4
    ctx.out = ctx.data.nested["value"]
    graph = ctx.get_graph()
    add_entry = next(entry for entry in graph["nodes"] if entry["path"] == ["add"])
    assert add_entry["call_mode"] == "delayed"
    assert graph["connections"][-1]["source"] == ["data", "nested", "value"]

    clone = Context()
    clone.set_graph(graph)
    assert type(clone.add).__name__ == "Transformer"
    assert clone.add.run() == 7
    assert clone.out.value == 2

    direct_ctx = Context()
    direct_ctx.add = direct(add)
    direct_ctx.add.pins.x = 1
    direct_ctx.add.pins.y = 2
    direct_graph = direct_ctx.get_graph()
    direct_clone = Context()
    direct_clone.set_graph(direct_graph)
    assert type(direct_clone.add).__name__ == "DirectTransformer"
