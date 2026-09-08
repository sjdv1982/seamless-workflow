"""Callable replacement preserves shared pins and disconnects removed pins."""
import pytest

from seamless_transformer import delayed


def original(x, y):
    return x + y


def same(x, y):
    return x * y


def reduced(x):
    return x * 10


def changed(x, z):
    return x - z


def expanded(x, y, z):
    return x + y + z


def optional(x, z=None):
    return x + (7 if z is None else z)


def no_pins():
    return 99


def assign(ctx, replacement, route):
    if route == "node":
        ctx.tf = replacement
    else:
        ctx.tf.code = replacement


def setup(ctx, binding):
    ctx.source_x = 6
    ctx.source_y = 4
    ctx.tf = original
    ctx.tf.pins.x = ctx.source_x if binding in {"edges", "mixed"} else 6
    ctx.tf.pins.y = ctx.source_y if binding == "edges" else 4
    ctx.out = ctx.tf
    ctx.compute(timeout=10)
    assert ctx.out.value == 10


def graph_node(ctx):
    return next(n for n in ctx.get_graph()["nodes"] if n["path"] == ["tf"])


@pytest.mark.parametrize("route", ["node", "code"])
@pytest.mark.parametrize("binding", ["inline", "edges", "mixed"])
@pytest.mark.parametrize("replacement, pins, missing, expected", [
    (same, {"x", "y"}, [], 24),
    (reduced, {"x"}, [], 60),
    (changed, {"x", "z"}, ["z"], 3),
    (expanded, {"x", "y", "z"}, ["z"], 13),
    (optional, {"x", "z"}, [], 13),
    (no_pins, set(), [], 99),
])
def test_callable_signature_replacement(make_context, route, binding, replacement, pins, missing, expected):
    ctx = make_context()
    setup(ctx, binding)
    old_handle = ctx.tf
    assign(ctx, replacement, route)
    if replacement is optional:
        # Seamless optionality is explicit, independent of Python defaults.
        ctx.tf.optional_pins = {"z"}
    node = graph_node(ctx)
    assert set(node["pins"]) == pins
    assert set(node["producers"]) <= pins
    for removed in {"x", "y"} - pins:
        assert removed not in dir(ctx.tf.pins)
        with pytest.raises(AttributeError):
            getattr(ctx.tf.pins, removed)
        assert not any(e["target"] == ["tf", removed] for e in ctx.get_graph()["connections"])
    if missing:
        assert ctx.tf.state == "unwired"
        assert ctx.tf.block_reason == missing
        ctx.tf.pins.z = 3
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == expected
    assert old_handle.result.value == expected
    assert ctx.out.value == expected
    # Surviving cell connections remain reactive after replacement.
    if "x" in pins and binding in {"edges", "mixed"}:
        ctx.source_x = 8
        ctx.compute(timeout=10)
        arguments = {"x": 8}
        if "y" in pins:
            arguments["y"] = 4
        if missing:
            arguments["z"] = 3
        assert ctx.out.value == replacement(**arguments)


@pytest.mark.parametrize("route", ["node", "code"])
@pytest.mark.parametrize("binding", ["inline", "edges", "mixed"])
def test_removed_pins_do_not_reappear_when_signature_restored(make_context, route, binding):
    ctx = make_context()
    setup(ctx, binding)
    assign(ctx, reduced, route)
    ctx.compute(timeout=10)
    assign(ctx, original, route)
    assert ctx.tf.state == "unwired"
    assert ctx.tf.block_reason == ["y"]
    assert ctx.tf.pins.y is None
    ctx.source_y = 100
    assert ctx.tf.state == "unwired"
    ctx.tf.pins.y = 2
    ctx.compute(timeout=10)
    assert ctx.out.value == 8


@pytest.mark.parametrize("old_binding", ["inline", "edges"])
def test_explicit_builder_values_override_retained_inputs(make_context, old_binding):
    ctx = make_context()
    setup(ctx, old_binding)
    replacement = delayed(same)
    replacement.pins.x = 3
    ctx.tf = replacement
    ctx.compute(timeout=10)
    assert ctx.out.value == 12
    ctx.source_x = 100
    ctx.compute(timeout=10)
    assert ctx.out.value == 12


@pytest.mark.parametrize("route", ["node", "code"])
def test_removed_inline_values_release_reference_holds(make_context, route):
    from seamless import Buffer
    from seamless.caching.buffer_cache import get_buffer_cache
    from seamless.reference_lifecycle import audit_reference_accounting

    ctx = make_context()
    ctx.tf = original
    x = Buffer(611, "mixed").get_checksum()
    y = Buffer(419, "mixed").get_checksum()
    ctx.tf.pins.x = x
    ctx.tf.pins.y = y
    ctx.compute(timeout=10)
    assign(ctx, reduced, route)
    ctx.compute(timeout=10)
    snapshot = get_buffer_cache().reference_snapshot()
    assert snapshot.get(x, (0,))[0] == 1
    assert snapshot.get(y, (0,))[0] == 0
    audit_reference_accounting(holders=[ctx])
    ctx.close()
    assert get_buffer_cache().reference_snapshot().get(x, (0,))[0] == 0


@pytest.mark.parametrize("route", ["node", "code"])
def test_unwired_shared_pin_stays_unwired(make_context, route):
    ctx = make_context()
    ctx.tf = original
    ctx.tf.pins.y = 4
    assign(ctx, same, route)
    assert ctx.tf.state == "unwired"
    assert ctx.tf.block_reason == ["x"]
    assert ctx.tf.pins.y == 4
    ctx.tf.pins.x = 6
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 24
