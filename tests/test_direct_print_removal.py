"""Phase B acceptance for removed workflow builder and graph state."""

import copy
import dataclasses

import pytest

from seamless_workflow.configuration import TransformerConfig


def identity(value):
    return value


def test_bound_direct_print_attribute_is_rejected(make_context):
    ctx = make_context()
    ctx.tf = identity
    with pytest.raises(AttributeError):
        getattr(ctx.tf, "direct_print")
    with pytest.raises(AttributeError):
        ctx.tf.direct_print = True


def test_workflow_config_has_no_direct_print_field():
    assert "direct_print" not in {field.name for field in dataclasses.fields(TransformerConfig)}


@pytest.mark.parametrize("old_value", [False, True])
def test_graph_omits_direct_print_and_ignores_legacy_key(make_context, old_value):
    ctx = make_context()
    ctx.tf = identity
    graph = ctx.get_graph()
    node = next(node for node in graph["nodes"] if node["path"] == ["tf"])
    assert "direct_print" not in node
    legacy = copy.deepcopy(graph)
    legacy_node = next(node for node in legacy["nodes"] if node["path"] == ["tf"])
    legacy_node["direct_print"] = old_value
    restored = make_context()
    restored.set_graph(legacy)
    assert restored.get_graph() == graph
    assert restored.tf.streaming is False
