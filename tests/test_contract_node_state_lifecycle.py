"""Contract tests for ``contracts/node-state-lifecycle.md`` (feature 10).

Each test names the section of the contract page it pins.  Gaps where the code
is behind the contract are ``xfail(strict=False)`` with the section in the
reason.  One pytest process per file (see run-tests.sh).
"""

from __future__ import annotations

import time
from typing import get_args

import pytest
from seamless import Cell
from seamless_workflow import Context
from seamless_workflow.errors import NodeError
from seamless_workflow.graph import BlockReason, NodeState


DOC = "node-state-lifecycle.md"

# Precedence, highest first (§Block reasons / Precedence).
PRECEDENCE = [
    "miswired",
    "unwired",
    "blocked-by-miswiring",
    "blocked-by-unwired",
    "blocked-by-error",
    "waiting",
]


def winner(reasons: dict) -> str:
    return min(reasons.values(), key=PRECEDENCE.index)


def gap(section, why):
    return pytest.mark.xfail(strict=False, reason=f"{DOC} {section}: {why}")


# ------------------------------------------------------------------ bodies


def double(x):
    return 2 * x


def triple(x):
    return 3 * x


def add(x, y):
    return x + y


def inc(x):
    return x + 1


def optional_add(x, y=None):
    return x if y is None else x + y


def boom(x):
    raise RuntimeError("boom")


def slow_boom(x):
    import time

    time.sleep(0.5)
    raise RuntimeError("boom")


def sleepy(x):
    import time

    time.sleep(x)
    return x


def slow_double(x):
    import time

    time.sleep(0.5)
    return 2 * x


def slow_triple(x):
    import time

    time.sleep(0.5)
    return 3 * x


def slow_inc(x):
    import time

    time.sleep(0.3)
    return x + 1


# ------------------------------------------------------------------ helpers


def run_record(ctx, name):
    return next(
        n for n in ctx.get_graph(runtime=True)["nodes"] if n["path"] == [name]
    )["runtime"]["run"]


def wait_started(ctx, name, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = run_record(ctx, name)["current"]
        if current and current["identity"]:
            return
        time.sleep(0.01)
    raise AssertionError(f"{name}: submission did not construct")


def miswired_transformer(ctx, name="mis"):
    """A transformer whose pin ``x`` is a projection of a retyped source.

    Built valid (source ``mixed`` == pin celltype ``mixed``), then the source is
    retyped: a valid request that invalidates someone else's wiring
    (§The seven states, *miswired is a static defect*).
    """
    ctx.src = Cell("mixed")
    ctx.src.set([1, 2])
    setattr(ctx, name, double)
    getattr(ctx, name).pins.x = ctx.src[1]
    ctx.compute(timeout=10)
    assert getattr(ctx, name).state == "complete"
    ctx.src.celltype = "plain"
    return getattr(ctx, name)


# ------------------------------------------------ vocabulary (§The seven states)


def test_vocabulary_is_seven_states_and_three_literal_block_reasons():
    """§The seven states + §Block reasons: Literal aliases, not enums."""
    assert set(get_args(NodeState)) == {
        "unwired", "miswired", "blocked", "waiting", "computing", "complete", "failed",
    }
    assert set(get_args(BlockReason)) == {
        "blocked-by-unwired", "blocked-by-error", "blocked-by-miswiring",
    }
    assert all(isinstance(member, str) for member in get_args(BlockReason))


# ---------------------------------- failure and unwired cones (replace stale A0)


def test_failure_cone_is_blocked_by_error_with_dict_reasons(make_context):
    """Rule 3 + §Where each form is visible + §Transitivity.

    Current-format replacement for node-transition/test_transition_failure.py,
    which still asserts the retired list-of-pins ``block_reason``.
    """
    ctx = make_context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail
    ctx.out = ctx.tail
    ctx.further = double
    ctx.further.pins.x = ctx.out
    ctx.compute(timeout=10)

    assert ctx.fail.state == "failed"
    assert ctx.fail.result.checksum is None
    assert ctx.tail.state == "blocked"
    assert ctx.tail.block_reason == {"x": "blocked-by-error"}
    assert ctx.out.state == "blocked"
    assert ctx.out.block_reason == "blocked-by-error"
    assert ctx.further.block_reason == {"x": "blocked-by-error"}


def test_only_the_failing_node_reports_the_exception(make_context):
    """§Leaving a state: only the failing node reports; node == result handle."""
    ctx = make_context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail
    ctx.out = ctx.tail
    ctx.compute(timeout=10)

    assert isinstance(ctx.fail.exception, str)
    assert "boom" in ctx.fail.exception
    assert ctx.fail.result.state == "failed"
    assert ctx.fail.result.exception == ctx.fail.exception
    for handle in (ctx.tail, ctx.tail.result, ctx.out):
        assert handle.state == "blocked"
        assert handle.exception is None


def test_unwired_cone_is_blocked_by_unwired_with_dict_reasons(make_context):
    """§Transformer nodes step 1 + §Transitivity (replaces stale unwired tests)."""
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.mid = double
    ctx.mid.pins.x = ctx.tf
    ctx.tail = double
    ctx.tail.pins.x = ctx.mid
    ctx.out = ctx.tail

    assert ctx.tf.state == "unwired"
    assert ctx.tf.block_reason == {"y": "unwired"}
    assert ctx.mid.state == "blocked"
    assert ctx.mid.block_reason == {"x": "blocked-by-unwired"}
    assert ctx.tail.block_reason == {"x": "blocked-by-unwired"}
    assert ctx.out.block_reason == "blocked-by-unwired"
    assert ctx.mid.exception is None


@pytest.mark.parametrize("kind", ["unwired", "error", "miswiring"])
def test_every_reason_propagates_ten_edges_down(make_context, kind):
    """§Transitivity: 'a node ten edges below a missing wire still says ...'."""
    ctx = make_context()
    if kind == "unwired":
        ctx.t0 = add
        ctx.t0.pins.x = 1
    elif kind == "error":
        ctx.t0 = boom
        ctx.t0.pins.x = 1
    else:
        miswired_transformer(ctx, "t0")
    previous = ctx.t0
    for i in range(1, 11):
        setattr(ctx, f"t{i}", double)
        node = getattr(ctx, f"t{i}")
        node.pins.x = previous
        previous = node
    if kind != "miswiring":
        # A cell below blocked-by-miswiring never quiesces; that gap is pinned
        # by test_cell_downstream_of_a_miswired_transformer_is_blocked_by_miswiring.
        ctx.end = ctx.t10
    ctx.compute(timeout=10)

    expected = f"blocked-by-{kind}"
    if kind != "miswiring":
        assert ctx.end.block_reason == expected
    for i in range(1, 11):
        node = getattr(ctx, f"t{i}")
        assert node.state == "blocked", (i, node.state)
        assert node.block_reason == {"x": expected}, (i, node.block_reason)
        assert node.exception is None


# -------------------------------------------------- precedence, live graphs


def test_upstream_unwired_beats_upstream_error_on_a_live_transformer(make_context):
    """§Precedence: topology before values (ruled 2026-09-18)."""
    ctx = make_context()
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.loose = add
    ctx.loose.pins.x = 1
    ctx.j = add
    ctx.j.pins.x = ctx.bad
    ctx.j.pins.y = ctx.loose
    ctx.compute(timeout=10)

    assert ctx.j.state == "blocked"
    reasons = ctx.j.block_reason
    assert reasons == {"x": "blocked-by-error", "y": "blocked-by-unwired"}
    assert winner(reasons) == "blocked-by-unwired"
    assert ctx.j.result.block_reason == "blocked-by-unwired"


def test_upstream_miswiring_beats_upstream_unwired_on_a_live_transformer(make_context):
    """§Precedence: blocked-by-miswiring > blocked-by-unwired."""
    ctx = make_context()
    miswired_transformer(ctx)
    ctx.loose = add
    ctx.loose.pins.x = 1
    ctx.j = add
    ctx.j.pins.x = ctx.mis
    ctx.j.pins.y = ctx.loose
    ctx.compute(timeout=10)

    assert ctx.mis.state == "miswired"
    assert ctx.j.state == "blocked"
    reasons = ctx.j.block_reason
    assert reasons == {"x": "blocked-by-miswiring", "y": "blocked-by-unwired"}
    assert winner(reasons) == "blocked-by-miswiring"


def test_waiting_loses_to_a_blocking_reason(make_context):
    """§Precedence: one progressing input + one blocked input = blocked."""
    ctx = make_context()
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.compute(timeout=10)
    ctx.slow = sleepy
    ctx.slow.pins.x = 2
    ctx.j = add
    ctx.j.pins.x = ctx.slow
    ctx.j.pins.y = ctx.bad

    assert ctx.slow.state == "computing"
    assert ctx.j.state == "blocked"
    assert ctx.j.block_reason == {"x": "waiting", "y": "blocked-by-error"}


# ------------------------------------------------ miswired (§The seven states)


def test_set_graph_derives_miswired_rather_than_rejecting(make_context):
    """§The seven states: set_graph derives the state, so a graph can be handed on mid-repair."""
    ctx = make_context()
    miswired_transformer(ctx)
    graph = ctx.get_graph()

    restored = make_context()
    restored.set_graph(graph)
    assert restored.mis.state == "miswired"
    assert restored.mis.block_reason == {"x": "miswired"}
    assert restored.mis.exception is None


def test_writing_a_projected_and_converting_edge_directly_raises(make_context):
    """§The seven states: an invalid request raises instead of leaving `miswired`."""
    ctx = make_context()
    ctx.src = Cell("plain")
    ctx.src.set([1, 2])
    ctx.tf = double
    ctx.tf.pins.x.celltype = "int"
    with pytest.raises(TypeError):
        ctx.tf.pins.x = ctx.src[1]


@gap("§Transitivity / §Equilibrium", "a cell fed by a miswired transformer is left `waiting` "
     "(Context._apply_upstream_state has no miswired branch), so the graph never quiesces")
def test_cell_downstream_of_a_miswired_transformer_is_blocked_by_miswiring(make_context):
    ctx = make_context()
    miswired_transformer(ctx)
    ctx.out = ctx.mis
    ctx.compute(timeout=5)  # must quiesce: miswired/blocked are equilibrium states

    assert ctx.out.state == "blocked"
    assert ctx.out.block_reason == "blocked-by-miswiring"


@gap("§States as seen through barriers", "node barrier on a miswired node raises "
     "'Node is miswired: None', without the repair description naming the edge")
def test_node_barrier_on_miswired_names_the_edge(make_context):
    ctx = make_context()
    miswired_transformer(ctx)
    with pytest.raises(NodeError) as info:
        ctx.mis.compute(timeout=10)
    message = str(info.value)
    assert "miswired" in message
    assert "x" in message and "plain" in message and "mixed" in message


def test_node_barrier_on_miswired_raises_node_error(make_context):
    """§States as seen through barriers: miswired -> NodeError naming the state."""
    ctx = make_context()
    miswired_transformer(ctx)
    with pytest.raises(NodeError, match="miswired"):
        ctx.mis.compute(timeout=10)


@gap("§States as seen through barriers", "NodeError for an unwired transformer reads "
     "'Node is unwired: None' and does not name the missing pin")
def test_node_barrier_on_unwired_transformer_names_the_missing_pin(make_context):
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    with pytest.raises(NodeError) as info:
        ctx.tf.compute(timeout=10)
    assert "unwired" in str(info.value)
    assert "y" in str(info.value)


def test_graph_barrier_returns_on_failed_blocked_unwired_graph(make_context):
    """§States as seen through barriers: the graph barrier does not raise."""
    ctx = make_context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail
    ctx.loose = add
    ctx.loose.pins.x = 1
    ctx.compute(timeout=10)  # must not raise
    assert ctx.fail.state == "failed"
    assert isinstance(ctx.fail.exception, str)
    assert ctx.tail.state == "blocked"
    assert ctx.loose.state == "unwired"


# ---------------------------------- rule 3: a pin's own failure (§Transformer nodes)


@pytest.mark.parametrize("case", ["null-on-required-int", "text-to-int-conversion"])
def test_a_pins_own_failure_blocks_the_transformer_without_an_exception(make_context, case):
    """§Transformer nodes step 3: pin failed, node blocked-by-error, tf.exception None."""
    ctx = make_context()
    if case == "null-on-required-int":
        ctx.src = Cell("plain")
        ctx.src.set(None)
    else:
        ctx.src = Cell("text")
        ctx.src.set("not a number")
    ctx.tf = double
    ctx.tf.pins.x.celltype = "int"
    ctx.tf.pins.x = ctx.src
    ctx.compute(timeout=10)

    assert ctx.tf.state == "blocked"
    assert ctx.tf.block_reason == {"x": "blocked-by-error"}
    assert ctx.tf.exception is None
    assert ctx.tf.pins.x.state == "failed"
    assert isinstance(ctx.tf.pins.x.exception, str) and ctx.tf.pins.x.exception


# -------------------------------------------------- connectivity (optional pins)


def test_an_errored_connected_optional_upstream_blocks(make_context):
    """§Connectivity: optionality normalizes a successful null, never an error."""
    ctx = make_context()
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.tf = optional_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.y = ctx.bad
    ctx.compute(timeout=10)

    assert ctx.tf.state == "blocked"
    assert ctx.tf.block_reason == {"y": "blocked-by-error"}
    assert ctx.tf.result.checksum is None


def test_a_connected_optional_pin_gates_like_a_required_pin(make_context):
    """§Connectivity: a connected optional upstream still in progress holds the node waiting."""
    ctx = make_context()
    ctx.slow = sleepy
    ctx.slow.pins.x = 0.5
    ctx.tf = optional_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.y = ctx.slow

    assert ctx.tf.state == "waiting"
    assert ctx.tf.block_reason == {"y": "waiting"}
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 1.5


# -------------------------------------------------------- leaving a state


def test_clear_exception_is_a_noop_without_an_exception(make_context, transformation_observations):
    """§Leaving a state: clear_exception() is a no-op on a node with no exception."""
    ctx = make_context()
    ctx.tf = double
    ctx.tf.pins.x = 3
    ctx.compute(timeout=10)
    checksum = ctx.tf.result.checksum

    ctx.tf.clear_exception()
    assert ctx.tf.state == "complete"
    ctx.compute(timeout=10)
    assert ctx.tf.result.checksum == checksum
    assert len(transformation_observations.misses("tf")) == 1


def test_equilibrium_is_never_left_autonomously(make_context, transformation_observations):
    """§Leaving a state + §Non-goals: no retry, no ageing out."""
    ctx = make_context()
    ctx.fail = boom
    ctx.fail.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.fail
    ctx.compute(timeout=10)
    first = ctx.fail.exception

    time.sleep(1.0)
    ctx.compute(timeout=10)
    assert ctx.fail.state == "failed"
    assert ctx.fail.exception == first
    assert ctx.tail.state == "blocked"
    assert len(transformation_observations.misses("fail")) == 1


def test_an_identical_message_after_an_edit_is_a_new_failure(make_context, transformation_observations):
    """§Leaving a state: a failure is an event, not a string."""
    ctx = make_context()
    ctx.fail = slow_boom
    ctx.fail.pins.x = 1
    ctx.compute(timeout=10)
    assert ctx.fail.state == "failed"

    ctx.fail.pins.x = 2  # same code, same message, new identity
    assert ctx.fail.state != "failed"
    assert ctx.fail.exception is None
    ctx.compute(timeout=10)
    assert ctx.fail.state == "failed"
    assert "boom" in ctx.fail.exception
    assert len(transformation_observations.misses("fail")) == 2


# ------------------------------------------------------------ the cascade


def test_a_self_edit_revokes_the_node_and_its_cone(make_context):
    """§The cascade: 'leaves complete' includes self-edits (code change, no input change)."""
    ctx = make_context()
    ctx.tf = slow_double
    ctx.tf.pins.x = 2
    ctx.tail = slow_double
    ctx.tail.pins.x = ctx.tf
    ctx.out = ctx.tail
    ctx.compute(timeout=10)
    assert ctx.out.value == 8

    ctx.tf.code = slow_triple
    assert ctx.tf.state == "computing"
    assert ctx.tail.state == "waiting"
    assert ctx.out.state == "waiting"
    assert ctx.tail.result.checksum is None
    assert ctx.out.checksum is None
    ctx.compute(timeout=10)
    assert ctx.out.value == 12


def test_the_cascade_rederives_rather_than_force_sets(make_context):
    """§The cascade: a downstream with a failed co-input lands in blocked, not waiting."""
    ctx = make_context()
    ctx.a = 1
    ctx.ok = slow_double
    ctx.ok.pins.x = ctx.a
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.j = add
    ctx.j.pins.x = ctx.ok
    ctx.j.pins.y = ctx.bad
    ctx.compute(timeout=10)
    assert ctx.j.block_reason == {"y": "blocked-by-error"}

    ctx.a = 5
    assert ctx.ok.state == "computing"
    assert ctx.j.state == "blocked"
    assert ctx.j.block_reason == {"x": "waiting", "y": "blocked-by-error"}


def test_the_textbook_diamond_never_fires_on_a_stale_input(make_context, transformation_observations):
    """§The glitch-freedom invariant: a=1; b=a+1; c=a+b; a->10 gives c=21, never 12."""
    ctx = make_context()
    ctx.a = 1
    ctx.b = slow_inc
    ctx.b.pins.x = ctx.a
    ctx.c = add
    ctx.c.pins.x = ctx.a
    ctx.c.pins.y = ctx.b
    ctx.compute(timeout=10)
    assert ctx.c.result.value == 3

    ctx.a = 10
    assert ctx.b.state == "computing"
    assert ctx.c.state == "waiting"
    assert ctx.c.result.checksum is None
    ctx.compute(timeout=10)
    assert ctx.c.result.value == 21
    # Exactly two executions of c: (1, 2) and (10, 11).  A glitch would add (10, 2).
    assert len(transformation_observations.misses("c")) == 2, transformation_observations.entries()


# ------------------------------------------------ speculation, holds, prune


def test_quiescence_ignores_a_superseded_run_still_in_flight(make_context):
    """§Equilibrium: quiescent != cluster-idle; prune makes them coincide."""
    ctx = make_context()
    ctx.s = sleepy
    ctx.s.pins.x = 3
    wait_started(ctx, "s")

    start = time.monotonic()
    ctx.s.pins.x = 0
    ctx.compute(timeout=10)
    assert time.monotonic() - start < 2.0
    assert ctx.s.state == "complete"
    superseded = run_record(ctx, "s")["superseded"]
    assert [record["phase"] for record in superseded] == ["superseded"]

    states_before = ctx.s.state
    assert ctx.prune() == {"cancelled": 1}
    assert ctx.s.state == states_before
    assert run_record(ctx, "s")["superseded"] == []


def test_prune_leaves_the_current_run_and_all_states_untouched(make_context):
    """§prune: changes no node state; a computing node's current run is untouched."""
    ctx = make_context()
    ctx.s = sleepy
    ctx.s.pins.x = 0.5
    ctx.tail = double
    ctx.tail.pins.x = ctx.s
    wait_started(ctx, "s")

    assert ctx.prune() == {"cancelled": 0}
    assert ctx.s.state == "computing"
    assert ctx.tail.state == "waiting"
    ctx.compute(timeout=10)
    assert ctx.tail.result.value == 1.0


@gap("§(b) Self-edit revert hold", "window ruled 30 s; Scheduler.self_edit_hold_seconds is 15")
def test_the_self_edit_revert_window_is_thirty_seconds():
    from seamless_workflow.scheduler import Scheduler

    assert Scheduler().self_edit_hold_seconds == 30


# -------------------------------------------- cell joins (§Where each form is visible)


@gap("§Where each form is visible", "a cell with a root edge and a sub-path edge reports a bare "
     "enum, not a dict keyed by edge with '<root>' for the root edge")
def test_a_join_with_a_root_edge_names_it_root(make_context):
    ctx = make_context()
    ctx.base = Cell("plain")
    ctx.other = Cell("plain")
    ctx.join = Cell("plain")
    ctx.join = ctx.base
    ctx.join["k"] = ctx.other
    ctx.compute(timeout=10)

    assert ctx.join.state == "blocked"
    assert ctx.join.block_reason == {"<root>": "blocked-by-unwired", "k": "blocked-by-unwired"}
