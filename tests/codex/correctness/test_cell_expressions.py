from __future__ import annotations


def test_deep_source_projection_updates_a_subpath_target(make_context):
    """Port the deep-cell source/target cascade as value assertions."""

    context = make_context()
    context.source = {"payload": {"branch": {"leaf": 3}}}
    context.projected = context.source.payload.branch.leaf
    context.target = {"slot": -1, "untouched": "keep"}
    context.target.slot = context.projected

    assert context.projected.value == 3
    assert context.target.value == {"slot": 3, "untouched": "keep"}

    context.source = {"payload": {"branch": {"leaf": 11}}}

    assert context.projected.value == 11
    assert context.target.value == {"slot": 11, "untouched": "keep"}


def test_chained_projections_remain_reactive_at_every_depth(make_context):
    context = make_context()
    context.source = {"a": {"b": {"c": 5}}}
    context.level_one = context.source.a
    context.level_two = context.level_one.b
    context.level_three = context.level_two.c

    assert context.level_one.value == {"b": {"c": 5}}
    assert context.level_two.value == {"c": 5}
    assert context.level_three.value == 5

    context.source = {"a": {"b": {"c": 8}}}

    assert context.level_one.value == {"b": {"c": 8}}
    assert context.level_two.value == {"c": 8}
    assert context.level_three.value == 8


def test_wide_diamond_converges_without_losing_sibling_targets(make_context):
    context = make_context()
    context.source = {"payload": {"value": 4}}
    # Item syntax is required for a key named ``value`` because ``Cell.value``
    # is the public materialisation property.
    context.left = context.source.payload["value"]
    context.right = context.source.payload["value"]
    context.join = {"left": None, "right": None, "constant": "same"}
    context.join.left = context.left
    context.join.right = context.right
    context.result = context.join

    assert context.result.value == {
        "left": 4,
        "right": 4,
        "constant": "same",
    }

    context.source = {"payload": {"value": 9}}

    assert context.left.value == 9
    assert context.right.value == 9
    assert context.result.value == {
        "left": 9,
        "right": 9,
        "constant": "same",
    }
