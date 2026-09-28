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

# Precedence, highest first (§Block reasons / Precedence).  The five-member
# entry domain; a waiting input contributes no entry at all (ruling 4).
PRECEDENCE = [
    "miswired",
    "unwired",
    "blocked-by-miswiring",
    "blocked-by-unwired",
    "blocked-by-error",
]


def winner(reasons: dict) -> str:
    return min(reasons.values(), key=PRECEDENCE.index)


def gap(section, why):
    return pytest.mark.xfail(strict=False, reason=f"{DOC} {section}: {why}")


RULING_4_WAITING = gap("§Where each form is visible", "contract ahead of code: ruling 4 (2026-09-26): "
                      "a waiting input has no entry and a waiting node reports None; the code "
                      "lists waiting inputs with value 'waiting'")


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


@pytest.mark.parametrize("kind", ["unwired", "error", pytest.param("miswiring", marks=gap(
    "§Transitivity", "contract ahead of code: a cell below blocked-by-miswiring stays 'waiting' "
    "(Context._apply_upstream_state has no miswiring branch), so the graph never quiesces"))])
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
    ctx.end = ctx.t10
    ctx.compute(timeout=10)

    expected = f"blocked-by-{kind}"
    assert ctx.end.state == "blocked"
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


@RULING_4_WAITING
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
    assert ctx.j.block_reason == {"y": "blocked-by-error"}


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


@gap("§States as seen through barriers", "run() on a miswired node raises "
     "'Node is miswired: None', without the repair description naming the edge")
def test_run_on_miswired_names_the_edge(make_context):
    ctx = make_context()
    miswired_transformer(ctx)
    with pytest.raises(NodeError) as info:
        ctx.mis.run()
    message = str(info.value)
    assert "miswired" in message
    assert "x" in message and "plain" in message and "mixed" in message


def test_node_barrier_on_miswired_returns_none_and_run_raises_node_error(make_context):
    """§States as seen through barriers (ruled 2026-09-28): the barrier reports
    miswired as None; run() raises NodeError naming the state."""
    ctx = make_context()
    miswired_transformer(ctx)
    assert ctx.mis.compute(timeout=10) is None
    assert ctx.mis.state == "miswired"
    with pytest.raises(NodeError, match="miswired"):
        ctx.mis.run()


@gap("§States as seen through barriers", "NodeError for an unwired transformer reads "
     "'Node is unwired: None' and does not name the missing pin")
def test_run_on_unwired_transformer_names_the_missing_pin(make_context):
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    with pytest.raises(NodeError) as info:
        ctx.tf.run()
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


@RULING_4_WAITING
def test_a_connected_optional_pin_gates_like_a_required_pin(make_context):
    """§Connectivity: a connected optional upstream still in progress holds the node waiting."""
    ctx = make_context()
    ctx.slow = sleepy
    ctx.slow.pins.x = 0.5
    ctx.tf = optional_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.y = ctx.slow

    assert ctx.tf.state == "waiting"
    assert ctx.tf.block_reason is None
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


@RULING_4_WAITING
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
    assert ctx.j.block_reason == {"y": "blocked-by-error"}


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


@gap("§Where each form is visible", "contract ahead of code: ruling 4 (2026-09-26): a cell "
     "with one-level-deep inputs reports a dict keyed by edge; BoundCellBackend returns a scalar")
def test_a_join_reports_a_dict_keyed_by_edge(make_context):
    ctx = make_context()
    ctx.loose = Cell("plain")
    ctx.broken = Cell("str")
    ctx.broken.set("not an integer")
    ctx.broken.celltype = "int"
    ctx.join = Cell("plain")
    ctx.join["left"] = ctx.loose
    ctx.join["right"] = ctx.broken
    ctx.compute(timeout=10)

    assert ctx.join.state == "blocked"
    reasons = ctx.join.block_reason
    assert reasons == {"left": "blocked-by-unwired", "right": "blocked-by-error"}
    assert winner(reasons) == "blocked-by-unwired"


# ------------------------------------------------ rulings 5, 6, 8 (2026-09-26)


STAGE1_SCHEMA = """inputs:
  - {name: x, dtype: int32}
outputs:
  - {name: result, dtype: int32, shape: [K]}
"""


@gap("§How each state is derived", "contract ahead of code: ruling 6 (2026-09-26): a compiled "
     "Stage-1 failure is the transformer's own failure (state 'failed'); code reports 'blocked' with {}")
def test_a_compiled_stage1_failure_is_failed(make_context):
    from seamless_transformer import Transformer

    tf = Transformer("c", compiled=True)
    tf.schema = STAGE1_SCHEMA  # shape [K] without metavars: Stage-1 failure
    tf.celltypes.x = "int"
    tf.code = "int transform(int x, int *result) {return 0;}"
    ctx = make_context()
    ctx.tf = tf
    ctx.compute(timeout=10)

    assert ctx.tf.state == "failed"
    assert isinstance(ctx.tf.exception, str) and "metavars" in ctx.tf.exception
    assert ctx.tf.block_reason is None


@gap("§States as seen through barriers", "contract ahead of code: ruling 5 (2026-09-26): a "
     "standalone Pin can be miswired; after retyping its projected source it reports 'waiting'")
def test_a_standalone_pin_can_be_miswired():
    from seamless_transformer import delayed

    src = Cell("mixed")
    src.set([1, 2])
    tf = delayed(double)
    tf.pins.x = src[1]
    assert tf.pins.x.state == "complete"
    src.celltype = "plain"  # a valid request that invalidates the pin's wiring
    assert tf.pins.x.state == "miswired"
    assert tf.pins.x.checksum is None


def test_the_wiring_refusal_message_names_both_spellings(make_context):
    ctx = make_context()
    ctx.src = Cell("plain")
    ctx.src.set([1, 2])
    ctx.tf = double
    ctx.tf.pins.x.celltype = "int"
    with pytest.raises(TypeError) as info:
        ctx.tf.pins.x = ctx.src[1]
    message = str(info.value)
    assert "would convert plain -> int behind a projection" in message
    assert '[1].as_celltype("int")' in message
    assert '.as_celltype("int")[1]' in message


# ---------------------------- round-3 rulings (contract-clarity-rulings.md, line 70 on)


def _root_plus_subpath_refused():
    return gap("§Cell nodes (root edge and sub-path edges are mutually exclusive)",
               "contract ahead of code: a cell with sub-path edges "
               "may hold only a checksum at the root, never a root source; the code accepts both "
               "orders (and root-after-sub-path silently drops the sub-path edge)")


@_root_plus_subpath_refused()
def test_a_sub_path_edge_is_refused_on_a_cell_with_a_root_edge(make_context):
    ctx = make_context()
    ctx.base = Cell("plain")
    ctx.base.set({"a": 1})
    ctx.other = Cell("plain")
    ctx.other.set(2)
    ctx.join = Cell("plain")
    ctx.join = ctx.base
    # The exception class is not ruled yet; only the refusal is.
    with pytest.raises(Exception):
        ctx.join["k"] = ctx.other
    ctx.compute(timeout=10)
    assert ctx.join.value == {"a": 1}


@_root_plus_subpath_refused()
def test_a_root_edge_is_refused_on_a_cell_with_sub_path_edges(make_context):
    ctx = make_context()
    ctx.base = Cell("plain")
    ctx.base.set({"a": 1})
    ctx.other = Cell("plain")
    ctx.other.set(2)
    ctx.join = Cell("plain")
    ctx.join["k"] = ctx.other
    # The exception class is not ruled yet; only the refusal is.
    with pytest.raises(Exception):
        ctx.join = ctx.base
    ctx.compute(timeout=10)
    assert ctx.join.value == {"k": 2}


def test_a_root_checksum_with_sub_path_edges_is_accepted(make_context):
    """The ruling's positive half: the root may hold a checksum (a literal)."""
    ctx = make_context()
    ctx.other = Cell("plain")
    ctx.other.set(2)
    ctx.join = Cell("plain")
    ctx.join.set({"kept": True})
    ctx.join["k"] = ctx.other
    ctx.compute(timeout=10)
    assert ctx.join.value == {"kept": True, "k": 2}


COMPILED_SCHEMA = """inputs:
  - {name: x, dtype: int32}
outputs:
  - {name: result, dtype: int32}
"""
needs_gcc = pytest.mark.skipif(not __import__("shutil").which("gcc"), reason="gcc required")


def compiled_builder(celltype):
    from seamless_transformer import Transformer

    tf = Transformer("c", compiled=True)
    tf.schema = COMPILED_SCHEMA
    tf.celltypes.x = celltype
    tf.code = "#include <stdint.h>\nint transform(int32_t x, int32_t *result) {*result=x;return 0;}"
    return tf


@needs_gcc
@gap("§Transformer nodes, rule 3", "contract ahead of code: a pin with no valid checksum never sets "
     "tf.exception (round-3 ruling); the compiled conversion path sets it")
def test_a_compiled_pin_without_a_valid_checksum_blocks_without_an_exception(make_context):
    ctx = make_context()
    ctx.tf = compiled_builder("int")
    ctx.src = Cell("plain")
    ctx.src.set("abc")
    ctx.tf.pins.x = ctx.src
    ctx.compute(timeout=60)
    assert ctx.tf.state == "blocked"
    assert ctx.tf.block_reason == {"x": "blocked-by-error"}
    assert ctx.tf.exception is None
    assert ctx.tf.pins.x.state == "failed"


@needs_gcc
@gap("§Transformer nodes, rule 3", "contract ahead of code: a valid checksum the compiled transformer "
     "cannot use makes it 'failed' (round-3 ruling); the code reports blocked-by-error")
def test_a_compiled_transformer_that_cannot_use_a_valid_checksum_is_failed(make_context):
    ctx = make_context()
    ctx.tf = compiled_builder("mixed")
    ctx.src = Cell("mixed")
    ctx.src.set([1, 2])  # a valid mixed checksum; not an int32 scalar
    ctx.tf.pins.x = ctx.src
    ctx.compute(timeout=60)
    assert ctx.tf.state == "failed"
    assert ctx.tf.block_reason is None
    assert isinstance(ctx.tf.exception, str) and "'x'" in ctx.tf.exception


@needs_gcc
def test_a_compiled_out_of_range_value_is_a_transformer_failure(make_context):
    """Round-3 ruling: all pins valid, the transformer cannot use one -> failed."""
    ctx = make_context()
    ctx.tf = compiled_builder("mixed")
    ctx.src = Cell("mixed")
    ctx.src.set(2**40)
    ctx.tf.pins.x = ctx.src
    ctx.compute(timeout=60)
    assert ctx.tf.state == "failed"
    assert ctx.tf.block_reason is None
    assert isinstance(ctx.tf.exception, str) and ctx.tf.exception


# ------------------------------------------- coverage pass 2026-09-26 (post-rewrite)


def identity(x):
    return x


def test_a_local_miswiring_wins_over_upstream_reasons(make_context):
    """§Precedence: a local defect wins outright over upstream reasons; the dict
    still lists every input that is neither complete nor waiting."""
    ctx = make_context()
    ctx.src = Cell("mixed")
    ctx.src.set([1, 2])
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.tf = add
    ctx.tf.pins.x = ctx.src[1]
    ctx.tf.pins.y = ctx.bad
    ctx.compute(timeout=10)
    ctx.src.celltype = "plain"

    assert ctx.tf.state == "miswired"
    reasons = ctx.tf.block_reason
    assert reasons == {"x": "miswired", "y": "blocked-by-error"}
    assert winner(reasons) == "miswired"
    assert ctx.tf.exception is None


def test_a_cells_own_conversion_failure_is_failed(make_context):
    """§Cell nodes: a cell's own conversion failure is `failed` (not `blocked`),
    and only that cell reports the exception; its dependents are blocked-by-error."""
    ctx = make_context()
    ctx.a = Cell("text")
    ctx.a.set("abc")
    ctx.b = Cell("int")
    ctx.b = ctx.a
    ctx.c = ctx.b
    ctx.tf = double
    ctx.tf.pins.x = ctx.b
    ctx.compute(timeout=10)

    assert ctx.b.state == "failed"
    assert isinstance(ctx.b.exception, str) and ctx.b.exception
    assert ctx.b.block_reason is None
    assert ctx.c.state == "blocked"
    assert ctx.c.block_reason == "blocked-by-error"
    assert ctx.c.exception is None
    assert ctx.tf.state == "blocked"
    assert ctx.tf.block_reason == {"x": "blocked-by-error"}
    assert ctx.tf.exception is None


def test_cells_are_never_computing_and_non_blocked_nodes_report_no_block_reason(make_context):
    """Rules 1 and 2 + §Where each form is visible.

    A literal-pin transformer goes straight to `computing`; a cell node's own work
    (conversion, join) reports `waiting`, never `computing`; a waiting,
    computing, complete or failed node reports block_reason None.
    """
    ctx = make_context()
    ctx.s = sleepy
    ctx.s.pins.x = 0.7
    ctx.conv = Cell("int")
    ctx.conv = ctx.s
    ctx.join = Cell("plain")
    ctx.join["k"] = ctx.s
    ctx.fail = boom
    ctx.fail.pins.x = 1
    assert ctx.s.state == "computing"  # never observed waiting

    seen = set()
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        for name in ("s", "conv", "join"):
            node = getattr(ctx, name)
            seen.add((name, node.state, repr(node.block_reason)))
        time.sleep(0.005)
    ctx.compute(timeout=10)

    assert not any(state == "computing" for name, state, _ in seen if name != "s")
    assert ("conv", "waiting", "None") in seen
    assert ("join", "waiting", "None") in seen
    assert all(reason == "None" for _, _, reason in seen)
    for handle in (ctx.s, ctx.conv, ctx.join, ctx.s.result):
        assert handle.state == "complete"
        assert handle.block_reason is None
    assert ctx.fail.state == "failed"
    assert ctx.fail.block_reason is None
    assert ctx.fail.result.block_reason is None


def test_node_barrier_on_blocked_returns_none_and_run_names_the_block_reason(make_context):
    """§States as seen through barriers (ruled 2026-09-28): the barrier reports
    `blocked` as None; run() raises NodeError naming the block reason."""
    ctx = make_context()
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.tail = double
    ctx.tail.pins.x = ctx.bad
    ctx.loose = add
    ctx.loose.pins.x = 1
    ctx.below = double
    ctx.below.pins.x = ctx.loose
    ctx.compute(timeout=10)

    assert ctx.tail.compute(timeout=10) is None
    assert ctx.below.compute(timeout=10) is None
    with pytest.raises(NodeError) as info:
        ctx.tail.run()
    assert "blocked" in str(info.value) and "blocked-by-error" in str(info.value)
    with pytest.raises(NodeError) as info:
        ctx.below.run()
    assert "blocked-by-unwired" in str(info.value)


def test_two_handles_onto_one_named_node_report_the_same_state(make_context):
    """Top of page: state is a property of the node, not of the handle."""
    ctx = make_context()
    ctx.s = sleepy
    ctx.s.pins.x = 0.3
    first, second = ctx.s, ctx.s
    assert first.state == second.state == "computing"
    ctx.compute(timeout=10)
    assert first.state == second.state == "complete"


def test_a_standalone_cell_can_be_miswired():
    """§The seven states (standalone consumers) + ruling 5: a standalone Cell
    `c = b[3]` reports `miswired` after `b` is retyped, with checksum None."""
    b = Cell("mixed")
    b.set([1, 2, 3, 4])
    c = b[3]
    assert c.state != "miswired"
    b.celltype = "plain"  # a valid request that invalidates c's link
    assert c.state == "miswired"
    assert c.checksum is None


_CELL_JOIN_PRECEDENCE = gap(
    "§Implementation status (block-reason precedence is inverted for cell nodes)",
    "contract ahead of code: Context._derive_cell passes only the first incomplete edge "
    "of a join to Context._apply_pending, which ranks blocked-by-error above blocked-by-unwired",
)


@_CELL_JOIN_PRECEDENCE
def test_a_join_label_is_the_maximum_over_all_its_edges(make_context):
    """§Precedence + §Transitivity for a cell node: a join whose first edge is
    errored and second is unwired is blocked-by-unwired, and its consumers say so."""
    ctx = make_context()
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.loose = add
    ctx.loose.pins.x = 1
    ctx.join = Cell("plain")
    ctx.join["a"] = ctx.bad  # first edge: error
    ctx.join["b"] = ctx.loose  # second edge: unwired
    ctx.copy = ctx.join
    ctx.tf = double
    ctx.tf.pins.x = ctx.join
    ctx.compute(timeout=10)

    assert ctx.join.state == "blocked"
    assert ctx.copy.block_reason == "blocked-by-unwired"
    assert ctx.tf.block_reason == {"x": "blocked-by-unwired"}


@_CELL_JOIN_PRECEDENCE
def test_a_join_with_a_waiting_edge_and_a_blocked_edge_is_blocked(make_context):
    """§Precedence: a waiting input loses to everything, for cells too."""
    ctx = make_context()
    ctx.bad = boom
    ctx.bad.pins.x = 1
    ctx.compute(timeout=10)
    ctx.slow = sleepy
    ctx.slow.pins.x = 1.0
    ctx.join = Cell("plain")
    ctx.join["a"] = ctx.slow  # first edge: still progressing
    ctx.join["b"] = ctx.bad
    ctx.copy = ctx.join

    assert ctx.slow.state == "computing"
    assert ctx.join.state == "blocked"
    assert ctx.copy.block_reason == "blocked-by-error"


def _deep_index(celltype_of_members="bytes"):
    from seamless import Buffer

    member = Buffer(b"hello", celltype_of_members)
    member.tempref()
    index = Buffer({"a": member.get_checksum().hex()}, "plain")
    index.tempref()
    return index.get_checksum()


_DEEP_TABLE = gap(
    "§The seven states (miswired, deep sources: the deep table)",
    "code/contract mismatch, not listed in §Implementation status: a link outside the deep "
    "table is not recognised as miswired; the pin conversion fails ('Illegal expression "
    "conversion: folder -> plain') and the transformer reports blocked-by-error",
)


@_DEEP_TABLE
def test_retyping_a_deep_source_outside_the_deep_table_leaves_the_consumer_miswired(make_context):
    ctx = make_context()
    ctx.src = Cell("deepfolder", checksum=_deep_index())
    ctx.tf = identity
    ctx.tf.pins.x.celltype = "plain"
    ctx.tf.pins.x = ctx.src  # deepfolder -> plain: legal
    ctx.compute(timeout=10)
    assert ctx.tf.state == "complete"

    ctx.src.celltype = "folder"  # valid request; folder -> plain is outside the table
    ctx.compute(timeout=10)
    assert ctx.src.state == "complete"
    assert ctx.tf.state == "miswired"
    assert ctx.tf.block_reason == {"x": "miswired"}
    assert ctx.tf.exception is None


@_DEEP_TABLE
def test_writing_a_deep_link_outside_the_deep_table_directly_raises(make_context):
    """§The seven states: writing an ill-formed link directly raises (class unspecified)."""
    ctx = make_context()
    ctx.src = Cell("folder", checksum=_deep_index())
    ctx.tf = identity
    ctx.tf.pins.x.celltype = "plain"
    with pytest.raises(Exception):
        ctx.tf.pins.x = ctx.src


@gap("§The seven states (blocked: not itself errored) / §Leaving a state (only the failing "
     "node reports the exception)", "code/contract mismatch, not listed in §Implementation "
     "status: Reactive._derive_transformer clears node.exception only when the node becomes "
     "unwired, and BoundTransformerBackend.exception exposes it for 'blocked', so a transformer "
     "that failed and is then blocked by a newly failed upstream keeps its stale exception")
def test_a_previously_failed_transformer_blocked_by_a_new_upstream_failure_has_no_exception(make_context):
    ctx = make_context()
    ctx.up = identity
    ctx.up.pins.x = 5
    ctx.tf = boom
    ctx.tf.pins.x = ctx.up
    ctx.compute(timeout=10)
    assert ctx.tf.state == "failed"

    ctx.up.code = triple_boom
    ctx.compute(timeout=10)
    assert ctx.up.state == "failed"
    assert ctx.tf.state == "blocked"
    assert ctx.tf.block_reason == {"x": "blocked-by-error"}
    assert ctx.tf.exception is None
    assert ctx.tf.exception == ctx.tf.result.exception


def triple_boom(x):
    raise ValueError("upstream boom")
