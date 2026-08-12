from __future__ import annotations

import asyncio

import pytest

from seamless import Cell
from seamless import Checksum
from seamless import Buffer
from seamless_transformer import delayed, direct
from seamless_transformer.transformation_class import Transformation
from seamless_workflow import Context
from seamless_workflow.endpoints import BoundEndpoint
from seamless_workflow.errors import PathError, ReadOnlyEndpointError, StaleWorkflowHandleError
from seamless_workflow.views import MissingView


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


def test_pins_is_the_only_transformer_input_namespace():
    def pins(scratch, inp):
        return (scratch, inp)

    ctx = Context()
    ctx.tf = pins
    ctx.tf.pins.scratch = "pin"
    ctx.tf.pins.inp = "input"

    # A pin named like an execution setting is unambiguous: the attribute is always
    # the setting, the pin is always in .pins.
    assert ctx.tf.scratch is False
    ctx.tf.scratch = True
    assert ctx.tf.scratch is True
    assert ctx.tf.pins.scratch == "pin"
    assert ctx.tf.pins.inp == ctx.tf.args.inp == "input"
    assert ctx.tf.run() == ["pin", "input"]

    # No attribute or item pin sugar in either direction.
    with pytest.raises(AttributeError):
        _ = ctx.tf.inp
    with pytest.raises(AttributeError):
        ctx.tf.inp = "other"
    with pytest.raises(AttributeError):
        del ctx.tf.inp
    with pytest.raises(TypeError):
        _ = ctx.tf["inp"]
    with pytest.raises(TypeError):
        ctx.tf["inp"] = "other"
    with pytest.raises(AttributeError):
        _ = ctx.tf.typo

    metadata = ctx.tf.meta
    metadata["changed"] = True
    assert "changed" not in ctx.tf.meta
    ctx.tf.meta = {"changed": True}
    assert ctx.tf.meta["changed"] is True
    assert isinstance(ctx.tf.code, Buffer)

    del ctx.tf.pins.scratch
    assert ctx.tf.pins.scratch is None


def test_unknown_transformer_attribute_raises_instead_of_navigating():
    ctx = Context()
    ctx.add = add
    ctx.sub = Context()
    ctx.sub.tf = add

    # A bound Transformer resolves attributes as a class, not as a Context path:
    # an unknown name is an error, never a namespace view.
    with pytest.raises(AttributeError):
        _ = ctx.add.whatever
    with pytest.raises(AttributeError):
        _ = ctx.sub.tf.whatever
    with pytest.raises(AttributeError):
        ctx.add.whatever = 5
    with pytest.raises(AttributeError):
        del ctx.add.whatever

    # Only a path whose owning node does not exist is still an unresolved namespace.
    assert isinstance(ctx.nonode.whatever, MissingView)


def test_bound_pin_writes_respect_the_transformer_signature():
    ctx = Context()
    ctx.add = add

    with pytest.raises(AttributeError):
        ctx.add.pins.typo = 1
    with pytest.raises(AttributeError):
        ctx.add.pins["typo"] = 1
    with pytest.raises(AttributeError):
        ctx.add.celltypes.typo = "int"
    with pytest.raises(AttributeError):
        ctx.add.pins.result = 1
    assert sorted(ctx._graph.nodes[("add",)].transformer_config.pins) == ["x", "y"]

    # Same rule as the standalone builder it was bound from.
    standalone = delayed(add)
    with pytest.raises(AttributeError):
        standalone.pins.typo = 1

    ctx.add.pins.x = 2
    ctx.add.pins.y = 3
    assert ctx.add.run() == 5

    # Code without a signature constrains nothing.
    ctx.add.code = "result = 42"
    ctx.add.pins.extra = 1
    assert "extra" in ctx._graph.nodes[("add",)].transformer_config.pins


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
