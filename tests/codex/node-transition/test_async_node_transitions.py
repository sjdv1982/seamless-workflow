from __future__ import annotations

from uuid import uuid4


def delayed_increment(value, delay, nonce):
    """A cache-resistant transformer whose in-flight state is observable."""

    import time

    time.sleep(delay)
    return value + 1


def increment(value):
    return value + 1


def explode(value):
    raise RuntimeError(f"boom: {value}")


def _node(context, name):
    return context._graph.nodes[(name,)]


def _assert_no_result(context, *names):
    for name in names:
        assert _node(context, name).current_checksum is None


def test_last_required_pin_starts_one_run_and_leaves_downstream_waiting(make_context):
    """A ready node computes off-controller; its unresolved cone waits.

    This chooses the recommended meaning of ``waiting`` in design section
    24.5: inputs are not concrete.  Submitted work is ``computing``.
    """

    context = make_context()
    context.first = delayed_increment
    context.second = increment
    context.first.pins.delay = 0.25
    context.first.pins.nonce = uuid4().hex
    context.second.pins.value = context.first

    assert _node(context, "first").state == "unwired"
    assert _node(context, "second").state == "blocked"
    assert _node(context, "second").block_reason == "blocked-by-unwired"

    context.first.pins.value = 10

    assert _node(context, "first").state == "computing"
    assert _node(context, "second").state == "waiting"
    _assert_no_result(context, "first", "second")


def test_edit_revokes_every_complete_node_in_the_downstream_cone(make_context):
    """No stale complete checksum survives the authoritative edit turn."""

    context = make_context()
    context.source = 1
    context.first = delayed_increment
    context.second = increment
    context.third = increment
    context.first.pins.delay = 0.25
    context.first.pins.nonce = uuid4().hex
    context.first.pins.value = context.source
    context.second.pins.value = context.first
    context.third.pins.value = context.second
    context.compute()

    assert [_node(context, name).state for name in ("first", "second", "third")] == [
        "complete",
        "complete",
        "complete",
    ]

    context.source = 20

    assert _node(context, "first").state == "computing"
    assert _node(context, "second").state == "waiting"
    assert _node(context, "third").state == "waiting"
    _assert_no_result(context, "first", "second", "third")


def test_failed_run_is_terminal_and_blocks_its_downstream_cone(make_context):
    """Port of legacy ``exception_upon_compute.py`` with stdout as assertions."""

    context = make_context()
    context.bad = explode
    context.after = increment
    context.after.pins.value = context.bad
    context.bad.pins.value = 7

    context.compute()

    assert _node(context, "bad").state == "failed"
    assert isinstance(context.bad.exception, RuntimeError)
    assert str(context.bad.exception) == "boom: 7"
    assert _node(context, "bad").current_checksum is None
    assert _node(context, "after").state == "blocked"
    assert _node(context, "after").block_reason == "blocked-by-error"
    assert _node(context, "after").current_checksum is None


def test_disconnected_required_pin_is_unwired_and_blocks_downstream(make_context):
    context = make_context()
    context.first = increment
    context.second = increment
    context.first.pins.value = 1
    context.second.pins.value = context.first
    context.compute()

    del context.first.pins.value

    assert _node(context, "first").state == "unwired"
    assert _node(context, "first").current_checksum is None
    assert _node(context, "second").state == "blocked"
    assert _node(context, "second").block_reason == "blocked-by-unwired"
    assert _node(context, "second").current_checksum is None
