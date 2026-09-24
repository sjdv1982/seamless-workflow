"""Contract coverage for ``seamless/docs/agent/contracts/workflow-context.md``.

Each test names the section of the contract page it pins.  Rules the page
states but the code does not (yet) honour are ``xfail(strict=False)`` with the
page section in the reason; the gap is a finding, not a test bug.

Node-state/block-reason rules live in ``contracts/node-state-lifecycle.md`` and
are deliberately not pinned here.
"""


import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from seamless import Buffer, Cell, Checksum
from seamless_workflow import Context
from seamless_workflow.errors import (
    ClosedContextError,
    ConcurrentUpdateError,
    ControllerFailedError,
    NodeError,
    PathError,
    StaleWorkflowHandleError,
    ValueUnavailableError,
)


DOC = "workflow-context.md"


def add_one(x):
    return x + 1


def ident(x):
    return x


def sleeping(x):
    import time
    time.sleep(2)
    return x


def boom(x):
    raise RuntimeError("boom-marker")


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- construction


def test_expression_execution_is_keyword_only_and_validated(make_context):
    """§Constructing and closing: keyword-only; any other value raises ValueError."""

    for value in ("auto", "local", "remote"):
        make_context(expression_execution=value)
    with pytest.raises(ValueError):
        make_context(expression_execution="eager")
    with pytest.raises(TypeError):
        make_context("auto")


def test_context_requires_an_open_seamless(tmp_path):
    """§Constructing and closing: a Context can only be constructed while Seamless is open."""

    script = tmp_path / "closed.py"
    script.write_text(
        "import seamless\n"
        "from seamless_workflow import Context\n"
        "seamless.close()\n"
        "try:\n"
        "    Context()\n"
        "except Exception as exc:\n"
        "    print('REFUSED', type(exc).__name__)\n"
        "else:\n"
        "    print('CONSTRUCTED')\n"
    )
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert "REFUSED" in result.stdout, result.stdout + result.stderr


def test_every_bound_handle_kind_raises_closed_context_error_after_close():
    """§Constructing and closing / error table: a bound handle used after close raises ClosedContextError."""

    ctx = Context()
    ctx.a = 1
    ctx.tf = add_one
    ctx.tf.pins.x = ctx.a
    ctx.compute(timeout=30)
    cell, tf, result = ctx.a, ctx.tf, ctx.tf.result
    ctx.close()
    for operation in (
        lambda: cell.value,
        lambda: cell.checksum,
        lambda: cell.compute(timeout=1),
        lambda: tf.state,
        lambda: result.value,
        lambda: ctx.a,
        lambda: ctx.compute(timeout=1),
        ctx.prune,
    ):
        with pytest.raises(ClosedContextError):
            operation()
    ctx.close()  # still idempotent


def test_context_manager_exit_closes():
    """§Constructing and closing: close() is also the context-manager exit."""

    with Context() as ctx:
        ctx.a = 1
        controller, side = ctx._controller, ctx._side
    assert not controller.thread.is_alive()
    assert not side.thread.is_alive()
    with pytest.raises(ClosedContextError):
        ctx.a = 2


# ------------------------------------------------------ nodes and namespaces


def test_handles_are_fresh_and_item_access_stringifies(make_context):
    """§Nodes, names and namespaces; handle identity carries no meaning."""

    ctx = make_context()
    ctx.a = 5
    assert ctx.a is not ctx.a
    assert ctx["a"].checksum == ctx.a.checksum
    ctx[7] = 3
    assert ctx["7"].value == 3
    assert [node["path"] for node in ctx.get_graph()["nodes"]] == [["7"], ["a"]]


def test_unknown_path_is_a_namespace_placeholder(make_context):
    """§Nodes, names and namespaces: ctx.foo.bar = 1 creates the node at ("foo", "bar")."""

    ctx = make_context()
    view = ctx.foo  # no error
    assert not isinstance(view, Cell)
    ctx.foo.bar = 1
    ctx.foo["baz"] = 2
    paths = [node["path"] for node in ctx.get_graph()["nodes"]]
    assert paths == [["foo", "bar"], ["foo", "baz"]]
    assert ctx.foo.bar.value == 1


def test_context_assignment_declares_a_namespace_not_a_runtime(make_context):
    """§Nodes, names and namespaces: ctx.sub = Context() declares a namespace of this Context."""

    ctx = make_context()
    ctx.sub = Context()
    ctx.sub.x = 4
    ctx.y = ctx.sub.x
    ctx.compute(timeout=10)
    assert ctx.y.value == 4
    assert not isinstance(ctx.sub, Context)
    assert [node["path"] for node in ctx.get_graph()["nodes"]] == [["sub", "x"], ["y"]]


def test_deleting_a_namespace_deletes_its_subtree_and_stales_handles(make_context):
    """§Nodes, names and namespaces: deleting a namespace path deletes its whole subtree."""

    ctx = make_context()
    ctx.ns.p.q = 1
    ctx.ns.r = 2
    ctx.keep = 3
    handle = ctx.ns.p.q
    del ctx.ns
    assert [node["path"] for node in ctx.get_graph()["nodes"]] == [["keep"]]
    with pytest.raises(StaleWorkflowHandleError):
        handle.value


def test_mounts_is_reserved_for_assignment_and_graph_nodes(make_context):
    """§Nodes, names and namespaces: `mounts` is reserved."""

    ctx = make_context()
    with pytest.raises(AttributeError, match="mounts is reserved for the Context mount API"):
        ctx.mounts = 1
    with pytest.raises(AttributeError, match="mounts is reserved"):
        ctx["mounts"] = 1
    graph = {
        "__seamless_workflow__": "0.4",
        "nodes": [{"type": "cell", "path": ["mounts"], "celltype": "int", "value": None}],
        "connections": [],
    }
    with pytest.raises(PathError):
        ctx.set_graph(graph, mounts=False)
    assert ctx.get_graph()["nodes"] == []


# ------------------------------------------------------ the assignment table


def test_bound_source_onto_transformer_converts_it_to_a_cell(make_context):
    """§What an assignment means: bound source on a transformer converts the node to a cell."""

    ctx = make_context()
    ctx.a = 1
    ctx.tf = sleeping
    ctx.tf.pins.x = 99
    assert _wait_for(lambda: ctx.tf.state == "computing")
    ctx.tf = ctx.a
    ctx.compute(timeout=30)
    assert isinstance(ctx.tf, Cell)
    assert ctx.tf.value == 1
    node = next(n for n in ctx.get_graph()["nodes"] if n["path"] == ["tf"])
    assert node["type"] == "cell"
    assert {"source": ["a"], "target": ["tf"]} in [
        {"source": c["source"], "target": c["target"]} for c in ctx.get_graph()["connections"]
    ]


def test_cell_builder_onto_transformer_is_a_node_error(make_context):
    """§What an assignment means: a standalone Cell on a transformer node is a NodeError."""

    ctx = make_context()
    ctx.tf = add_one
    with pytest.raises(NodeError):
        ctx.tf = Cell(celltype="int")
    assert ctx.get_graph()["nodes"][0]["type"] == "transformer"


@pytest.mark.xfail(
    strict=False,
    reason=f"{DOC} §What an assignment means: code raises TypeError(PreparedTransformer) "
    "instead of NodeError for a Transformer builder onto a cell node",
)
def test_transformer_builder_onto_cell_is_a_node_error(make_context):
    from seamless_transformer import delayed

    ctx = make_context()
    ctx.a = 1
    with pytest.raises(NodeError):
        ctx.a = delayed(ident)
    assert ctx.a.value == 1


@pytest.mark.xfail(
    strict=False,
    reason=f"{DOC} §What an assignment means: code raises TypeError(PreparedTransformer) "
    "instead of NodeError('Cannot replace a cell node with transformer code')",
)
def test_callable_onto_cell_is_a_node_error(make_context):
    ctx = make_context()
    ctx.a = 1
    with pytest.raises(NodeError, match="Cannot replace a cell node with transformer code"):
        ctx.a = ident
    assert ctx.a.value == 1


@pytest.mark.xfail(
    strict=False,
    reason=f"{DOC} error table (NodeError: assignment mismatches the node kind): "
    "a plain value onto a transformer node raises a bare AssertionError from ingress",
)
def test_value_onto_transformer_is_a_node_error(make_context):
    ctx = make_context()
    ctx.tf = add_one
    with pytest.raises(NodeError):
        ctx.tf = 5
    assert ctx.get_graph()["nodes"][0]["type"] == "transformer"


def test_code_string_onto_existing_transformer_sets_its_code(make_context):
    """§What an assignment means: a code string on an existing transformer sets its code."""

    ctx = make_context()
    ctx.tf = add_one
    ctx.tf.pins.x = 1
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 2
    # A pin-less script body: whether a code string keeps the old pins is a
    # transformers.md question (today whole-node assignment drops them).
    ctx.tf = "result = 42"
    ctx.compute(timeout=30)
    assert ctx.get_graph()["nodes"][0]["type"] == "transformer"
    assert ctx.tf.result.value == 42


def test_parallel_edges_into_two_pins_are_not_a_cycle(make_context):
    """§What an assignment means: parallel edges into different pins are accepted."""

    def pair(x, y):
        return x + y

    ctx = make_context()
    ctx.a = 3
    ctx.tf = pair
    ctx.tf.pins.x = ctx.a
    ctx.tf.pins.y = ctx.a
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 6


# ----------------------------------------------------------- the controller


def test_poisoned_context_refuses_every_operation_except_close(monkeypatch):
    """§The controller: an internal failure poisons ingress; close() still works, nothing else does."""

    ctx = Context()
    ctx.a = 1
    handle = ctx.a

    def fail(self):
        raise AssertionError("injected continuation failure")

    monkeypatch.setattr(Context, "_injected_failure", fail, raising=False)
    notification = ctx._controller.submit("_injected_failure", klass=5)
    with pytest.raises(AssertionError):
        notification.result(timeout=3)

    def assign():
        ctx.b = 2

    for operation in (assign, ctx.get_graph, lambda: handle.value, lambda: ctx.a, ctx.prune,
                      lambda: ctx.compute(timeout=1)):
        with pytest.raises(ControllerFailedError):
            operation()
    ctx.close()
    ctx.close()
    assert not ctx._controller.thread.is_alive()


def test_ordinary_reads_never_wait(make_context):
    """§The controller: bound reads report current availability and return None when unpublished."""

    ctx = make_context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 5
    assert _wait_for(lambda: ctx.tf.state == "computing")
    start = time.monotonic()
    assert ctx.tf.result.checksum is None
    assert ctx.tf.result.value is None
    assert time.monotonic() - start < 1
    assert ctx.tf.state == "computing"


# ----------------------------------------------------------------- barriers


def test_barrier_on_node_deleted_while_waiting_raises_stale(make_context):
    """§Barriers: a barrier on a path whose node has been deleted raises StaleWorkflowHandleError."""

    ctx = make_context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 1
    handle = ctx.tf
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(handle.compute, timeout=20)
        assert _wait_for(lambda: ctx._barriers)
        del ctx.tf
        with pytest.raises(StaleWorkflowHandleError):
            pending.result(timeout=10)
    with pytest.raises(StaleWorkflowHandleError):
        handle.compute(timeout=5)


def test_reading_barrier_raises_the_nodes_own_recorded_exception(make_context):
    """§Barriers: a reading barrier on a failed node raises the node's own recorded exception."""

    ctx = make_context()
    ctx.f = boom
    ctx.f.pins.x = 1
    with pytest.raises(Exception) as info:
        ctx.f.compute(timeout=60)
    assert ctx.f.state == "failed"
    assert "boom-marker" in str(info.value)
    assert str(info.value) == ctx.f.exception


def test_context_barrier_returns_none_on_a_failed_graph(make_context):
    """§Barriers / surface table: ctx.compute() returns None; quiescence is node states only."""

    ctx = make_context()
    ctx.f = boom
    ctx.f.pins.x = 1
    ctx.tail = add_one
    ctx.tail.pins.x = ctx.f
    assert ctx.compute(timeout=60) is None
    assert ctx.f.state == "failed"
    assert isinstance(ctx.f.exception, str)


# ------------------------------------------------------------------- writes


@pytest.mark.xfail(
    strict=False,
    reason=f"{DOC} §Writes through the Context: whole-checksum writes are not HashType-validated "
    "at write time; the mismatch only surfaces at a read",
)
def test_whole_checksum_write_is_validated_against_the_celltype(make_context):
    ctx = make_context()
    ctx.i = Cell(celltype="int")
    text = Buffer("hello", "text")
    checksum = text.get_checksum()
    with pytest.raises(Exception):
        ctx.i.set_checksum(checksum)
    assert ctx.i.checksum is None


def test_unresolvable_checksum_write_is_installed_without_resolving(make_context):
    """§Writes through the Context: the buffer need not be present."""

    ctx = make_context()
    ctx.j = Cell(celltype="int")
    absent = Checksum("c3" * 32)
    ctx.j.set_checksum(absent)
    assert ctx.j.checksum == absent


def test_subpath_write_gives_up_after_eight_lost_commits(make_context, monkeypatch):
    """§Writes through the Context: a losing commit retries at most eight times, then ConcurrentUpdateError."""

    import seamless_workflow.context as module

    ctx = make_context()
    ctx.a = {"x": 0, "n": 0}
    assign = module._assign_path
    attempts = []

    def always_interleave(root, path, value):
        attempts.append(1)
        ctx.a = {"x": 0, "n": len(attempts)}  # moves the base under the transaction
        return assign(root, path, value)

    monkeypatch.setattr(module, "_assign_path", always_interleave)
    with pytest.raises(ConcurrentUpdateError):
        ctx.a.x = 7
    assert len(attempts) == 8
    assert ctx.a.value == {"x": 0, "n": 8}


def test_subpath_write_into_unwired_cell(make_context):
    """§Writes through the Context: no root checksum, not waiting → ValueUnavailableError,
    except that a string/attribute path into an unwired cell starts from an empty mapping."""

    ctx = make_context()
    ctx.u = Cell(celltype="mixed")
    ctx.u.k = 3
    assert ctx.u.value == {"k": 3}
    ctx.v = Cell(celltype="mixed")
    ctx.v["k"] = 4
    assert ctx.v.value == {"k": 4}
    ctx.w = Cell(celltype="mixed")
    with pytest.raises(ValueUnavailableError):
        ctx.w[0] = 3
    assert ctx.w.checksum is None


# --------------------------------------------------------- speculation control


class _CancelRecorder:
    def __init__(self, monkeypatch):
        from seamless_transformer.transformation_cache import TransformationCache

        self.soft, self.hard = [], []
        soft, hard = TransformationCache.softcancel_by_checksum, TransformationCache.cancel_by_checksum
        recorder = self

        def record_soft(cache, tf_checksum, *args, **kwargs):
            recorder.soft.append(Checksum(tf_checksum))
            return soft(cache, tf_checksum, *args, **kwargs)

        def record_hard(cache, tf_checksum, *args, **kwargs):
            recorder.hard.append(Checksum(tf_checksum))
            return hard(cache, tf_checksum, *args, **kwargs)

        monkeypatch.setattr(TransformationCache, "softcancel_by_checksum", record_soft)
        monkeypatch.setattr(TransformationCache, "cancel_by_checksum", record_hard)


def _running_identity(ctx, path):
    def current():
        node = next(n for n in ctx.get_graph(runtime=True)["nodes"] if n["path"] == [path])
        run = node["runtime"]["run"]["current"]
        return run and run["identity"]

    assert _wait_for(current), "transformation never constructed"
    return current()


def test_deleting_a_running_node_only_softcancels(make_context, monkeypatch):
    """§Nodes / §Speculation control: deletion cancels softly; hard cancellation is never issued."""

    recorder = _CancelRecorder(monkeypatch)
    ctx = make_context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 424242
    identity = _running_identity(ctx, "tf")
    del ctx.tf
    assert _wait_for(lambda: Checksum(identity) in recorder.soft), recorder.soft
    assert recorder.hard == []


def test_prune_and_supersession_only_softcancel(make_context, monkeypatch):
    """§Speculation control: prune() softcancels superseded runs; the reactive layer never hard-cancels."""

    recorder = _CancelRecorder(monkeypatch)
    ctx = make_context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 515151
    first = _running_identity(ctx, "tf")
    ctx.tf.pins.x = 515152
    result = ctx.prune()
    assert set(result) == {"cancelled"} and result["cancelled"] >= 1
    assert _wait_for(lambda: Checksum(first) in recorder.soft), recorder.soft
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 515152
    assert recorder.hard == []


def test_close_only_softcancels(monkeypatch):
    """§Constructing and closing / §Speculation control: close cancels memberships, softly."""

    recorder = _CancelRecorder(monkeypatch)
    ctx = Context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 616161
    identity = _running_identity(ctx, "tf")
    ctx.close()
    assert _wait_for(lambda: Checksum(identity) in recorder.soft), recorder.soft
    assert recorder.hard == []


# -------------------------------------------------------- graph serialization


def test_get_graph_is_durable_and_carries_no_runtime_state(make_context):
    """§Graph serialization: get_graph() returns the durable graph, never runtime state."""

    ctx = make_context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 3
    ctx.out = ctx.tf
    assert _wait_for(lambda: ctx.tf.state == "computing")
    during = ctx.get_graph()
    ctx.compute(timeout=30)
    after = ctx.get_graph()
    assert during == after
    for node in after["nodes"]:
        assert "runtime" not in node
        assert "state" not in node and "exception" not in node


def test_set_graph_cancels_old_runs_and_never_applies_their_late_results(make_context, monkeypatch):
    """§Graph serialization: set_graph cancels every run; generations are never reused."""

    recorder = _CancelRecorder(monkeypatch)
    donor = make_context()
    donor.tf = sleeping
    donor.tf.pins.x = 2
    graph = donor.get_graph()

    ctx = make_context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 717171
    old = _running_identity(ctx, "tf")
    ctx.set_graph(graph, mounts=False)
    assert _wait_for(lambda: Checksum(old) in recorder.soft), recorder.soft
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 2
    time.sleep(2.5)  # let the old run's late completion arrive, if it was not cancelled
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 2
    assert recorder.hard == []


def test_graph_loading_never_executes_code(make_context, tmp_path):
    """§Graph serialization: graph loading never executes code to reconstruct callables."""

    marker = tmp_path / "executed"
    code = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "def f(x):\n"
        "    return x\n"
    )
    graph = {
        "__seamless_workflow__": "0.4",
        "nodes": [
            {
                "type": "transformer",
                "path": ["tf"],
                "language": "python",
                "call_mode": "delayed",
                "code": code,
                "pins": {"x": {"celltype": "mixed"}},
            }
        ],
        "connections": [],
    }
    ctx = make_context()
    ctx.set_graph(graph, mounts=False)
    assert ctx.get_graph()["nodes"][0]["type"] == "transformer"
    assert ctx.tf.state == "unwired"
    assert not marker.exists()
