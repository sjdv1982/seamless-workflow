"""Reads through a bound anonymous or projection handle.

Contract: contracts/cells.md, *Anonymous and projection handles* (and *Reads*,
*Projections*); contracts/expressions.md, *Application order*, *Fusion* and
*Placement*.  A handle's reads build the handle's own Expression -- every link,
path and conversion, in syntax order -- over the parent's checksum, and evaluate
it through the standalone resolution order, placed by the Context.
"""
import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

from seamless import Buffer, Cell, Expression
from seamless.checksum.expression import get_expression_cache


def _load_fake_remotes():
    source = (
        Path(__file__).resolve().parents[2]
        / "seamless-core/tests/helpers/fake_remotes.py"
    )
    spec = importlib.util.spec_from_file_location("seamless_core_fake_remotes", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fake_remotes = _load_fake_remotes()

LIST_TEXT = "[10, 20, 30, 40]"


def _text_list(ctx):
    ctx.b = Cell("text")
    ctx.b.set(LIST_TEXT)
    ctx.compute(timeout=10)
    return ctx.b


def _read(handle, operation):
    if operation == "checksum":
        return handle.checksum
    if operation == "compute":
        return handle.compute()
    if operation == "compute_async":
        return asyncio.run(handle.compute_async())
    if operation == "buffer":
        buffer = handle.buffer
        return None if buffer is None else buffer.get_checksum()
    if operation == "value":
        return handle.value
    if operation == "run":
        return handle.run()
    raise ValueError(operation)


# --- The handle's own Expression: every link, in syntax order ----------------------


@pytest.mark.parametrize(
    "operation", ["checksum", "compute", "compute_async", "buffer", "value", "run"]
)
def test_conversion_then_path_handle_reads_the_converted_value(make_context, operation):
    """cells.md, *Anonymous and projection handles*: "`x.checksum`, `x.buffer`,
    `x.value`, `x.compute()` and `x.run()` build the handle's Expression and
    evaluate it through the standalone resolution order."

    cells.md, *Projections*: "`cell.as_celltype("plain")[3]` converts the text
    to `plain` first, closing that Expression, and then selects item `3` of the
    resulting list — the integer `40`."
    """
    ctx = make_context()
    _text_list(ctx)
    handle = ctx.b.as_celltype("plain")[3]
    expected_checksum = Buffer(40, "plain").get_checksum()
    expected = {
        "checksum": expected_checksum,
        "compute": expected_checksum,
        "compute_async": expected_checksum,
        "buffer": expected_checksum,
        "value": 40,
        "run": 40,
    }[operation]
    assert _read(handle, operation) == expected
    assert handle.exception is None


@pytest.mark.parametrize(
    "celltype, value, target",
    [
        ("text", "hello", "plain"),
        ("text", "hello", "str"),
        ("yaml", "a: 1\n", "plain"),
    ],
)
def test_pathless_conversion_handle_compute_returns_the_converted_checksum(
    make_context, celltype, value, target
):
    """cells.md, *Anonymous and projection handles*: "`x.compute()` ... build the
    handle's Expression and evaluate it ... The Expression's input is the
    parent's **checksum**, not the parent."

    The handle `ctx.p.as_celltype(target)` has one pathless conversion link, so
    `compute()` returns the result of `(parent checksum, "", celltype, target)`,
    never the parent's own checksum.
    """
    ctx = make_context()
    ctx.p = Cell(celltype)
    ctx.p.set(value)
    ctx.compute(timeout=10)
    parent = ctx.p.checksum
    expected = Expression(parent, input_celltype=celltype, celltype=target).compute()
    assert expected != parent, "precondition: this conversion changes the checksum"
    handle = ctx.p.as_celltype(target)
    assert handle.compute() == expected
    assert handle.checksum == expected


def test_path_then_conversion_and_conversion_then_path_are_different_recipes(make_context):
    """cells.md, *Projections*: "`cell[3].as_celltype("plain")` selects item `3`
    of the **text** — the character `','` — and renders that as `plain`.
    `cell.as_celltype("plain")[3]` converts the text to `plain` **first** ... and
    then selects item `3` of the resulting **list** — the integer `40`. The two
    spellings are different recipes, and each means what it reads as."
    """
    ctx = make_context()
    b = _text_list(ctx)
    parent = b.checksum
    path_first = ctx.b[3].as_celltype("plain")
    conversion_first = ctx.b.as_celltype("plain")[3]

    assert path_first.value == ","
    assert conversion_first.value == 40

    # Each handle's checksum is that of its own recipe over the parent's checksum.
    assert path_first.checksum == Expression(
        parent, path="[3]", input_celltype="text", celltype="plain"
    ).compute()
    converted = Expression(parent, input_celltype="text", celltype="plain")
    assert conversion_first.checksum == Expression(
        converted, path="[3]", input_celltype="plain", celltype="plain"
    ).compute()
    assert path_first.checksum != conversion_first.checksum


def test_conversion_handle_state_and_failure_are_local_to_the_handle(make_context):
    """cells.md, *Anonymous and projection handles*: "`x.state` is passive and
    local to the handle. It never builds or runs the Expression: a fresh handle
    is `waiting` until a pulling read or `compute()` on *that handle* succeeds
    (`complete`) or fails (`failed`)", and "Failures live on the handle, exactly
    as for a standalone Cell ...: recorded in `x.exception` as a string, cleared
    by `x.clear_exception()`, and never seen by another handle."
    """
    ctx = make_context()
    ctx.t = Cell("text")
    ctx.t.set("hello")
    ctx.compute(timeout=10)

    good = ctx.t.as_celltype("str")
    assert good.state == "waiting"
    assert good.exception is None
    assert good.checksum == Buffer("hello", "str").get_checksum()
    assert good.state == "complete"
    assert ctx.t.as_celltype("str").state == "waiting"

    bad = ctx.t.as_celltype("int")
    assert bad.state == "waiting"
    assert bad.checksum is None
    assert bad.state == "failed"
    assert isinstance(bad.exception, str) and bad.exception
    with pytest.raises(Exception):
        bad.run()
    fresh = ctx.t.as_celltype("int")
    assert fresh.exception is None
    assert fresh.state == "waiting"
    assert ctx.t.state == "complete"
    assert ctx.t.exception is None
    bad.clear_exception()
    assert bad.exception is None
    assert ctx.t.state == "complete"


# --- The standalone resolution order, placed by the Context -------------------------


def _remote_parent(monkeypatch, ctx_factory, *, jobserver_available=True,
                   execution=None, gate=None, started=None):
    """A named parent whose buffer is on the (fake) hashserver only."""
    source = Buffer({"a": "remote"}, "plain")
    parent = source.get_checksum()
    content = source.content
    del source
    result = Buffer("remote", "str")
    result_checksum = result.get_checksum()
    key = (parent.hex(), "a", "plain", "str")
    calls, buffers, sent = [], {parent: content}, []
    fake_remotes.install_fake_remotes(
        monkeypatch, {}, {key: result_checksum}, calls, buffers=buffers,
        jobserver_available=jobserver_available,
        run_expression_gate=gate, run_expression_started=started,
    )
    jobserver_remote = sys.modules["seamless_remote.jobserver_remote"]
    fake_run_expression = jobserver_remote.run_expression

    async def run_expression(*args, scratch=False):
        sent.append(scratch)
        answer = await fake_run_expression(*args, scratch=scratch)
        if not scratch:  # the executing side writes the end result
            buffers[answer] = result.content
        return answer

    monkeypatch.setattr(jobserver_remote, "run_expression", run_expression)
    ctx = ctx_factory() if execution is None else ctx_factory(expression_execution=execution)
    ctx.b = Cell("plain")
    ctx.b.checksum = parent
    ctx.compute(timeout=10)
    assert ctx.b.checksum == parent
    fake_remotes.drop_buffer(parent)
    fake_remotes.drop_buffer(result_checksum)
    get_expression_cache().clear()
    calls.clear()
    return ctx, result_checksum, calls, sent


def test_handle_over_a_remote_parent_buffer_dispatches(make_context, monkeypatch):
    """cells.md, *Reads*, standalone resolution order: "6. the input buffer is
    elsewhere — dispatch the evaluation and wait", which a handle follows:
    "build the handle's Expression and evaluate it through the standalone
    resolution order above."
    """
    ctx, expected, calls, sent = _remote_parent(monkeypatch, make_context)
    handle = ctx.b["a"].as_celltype("str")
    assert handle.checksum == expected, handle.exception
    assert handle.exception is None
    assert "jobserver:run" in calls
    # A handle starts non-scratch, so its dispatch asks for the end result.
    assert sent == [False]
    assert handle.value == "remote"


def test_handle_over_a_remote_parent_buffer_without_a_server_materializes_it(
    make_context, monkeypatch
):
    """expressions.md, *Placement*: "5. local materialization from the
    hashserver, when no server is configured"; cells.md: a handle evaluates
    "through the standalone resolution order", so a parent buffer that is not in
    this process's memory is no reason to fail.
    """
    ctx, expected, calls, _ = _remote_parent(
        monkeypatch, make_context, jobserver_available=False
    )
    handle = ctx.b["a"].as_celltype("str")
    assert handle.value == "remote", handle.exception
    assert handle.checksum == expected
    assert handle.exception is None
    assert "hashserver:get" in calls
    assert "jobserver:run" not in calls


@pytest.mark.parametrize("execution", ["auto", "remote", "local"])
def test_handle_read_honours_the_context_expression_execution(
    make_context, monkeypatch, execution
):
    """expressions.md, *Placement*: "`"auto"` is the default on every path that
    evaluates an Expression: ... and workflow Context projections
    (`Context(expression_execution=...)` overrides it for one Context)", and
    "`"local"` never dispatches."

    The parent's buffer is local here, so `auto` evaluates locally; `remote`
    dispatches anyway. A `local` Context fetches a parent buffer from the
    hashserver and evaluates without dispatching.
    """
    ctx, expected, calls, _ = _remote_parent(monkeypatch, make_context, execution=execution)
    if execution == "local":
        handle = ctx.b["a"].as_celltype("str")
        assert handle.checksum == expected
        assert handle.state == "complete"
        assert handle.exception is None
        assert "hashserver:get" in calls
        assert "jobserver:run" not in calls
        return
    # Make the parent's buffer local again: placement alone now decides.
    Buffer({"a": "remote"}, "plain").tempref()
    handle = ctx.b["a"].as_celltype("str")
    assert handle.checksum == expected, handle.exception
    assert ("jobserver:run" in calls) is (execution == "remote")


def test_running_loop_refusal_is_not_a_handle_failure(make_context, monkeypatch):
    """cells.md, *Anonymous and projection handles*: "A running-loop refusal is
    not a failure. ... The standalone getter turns that into `None`, leaves
    `.exception` as `None` and the state as `waiting`, and dispatches nothing."
    """
    ctx, expected, calls, _ = _remote_parent(monkeypatch, make_context)
    handle = ctx.b["a"].as_celltype("str")

    async def read_in_loop():
        return handle.checksum, handle.exception, handle.state

    assert asyncio.run(read_in_loop()) == (None, None, "waiting")
    assert "jobserver:run" not in calls
    # Outside the loop the condition resolves itself.
    assert handle.checksum == expected
    assert handle.state == "complete"


def test_handle_compute_timeout_bounds_its_own_dispatched_evaluation(
    make_context, monkeypatch
):
    """cells.md, *Anonymous and projection handles*: "They wait only for the
    handle's **own** evaluation when it is dispatched; `x.compute(timeout=…)`
    bounds that wait, and expiry raises `TimeoutError`."
    """
    gate, started = threading.Event(), threading.Event()
    ctx, expected, calls, _ = _remote_parent(
        monkeypatch, make_context, gate=gate, started=started
    )
    handle = ctx.b["a"].as_celltype("str")
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            handle.compute(timeout=0.5)
        assert time.monotonic() - start < 5
        assert started.is_set()
        # Expiry is about the call, not the work: nothing is recorded.
        assert handle.exception is None
    finally:
        gate.set()
    assert handle.compute(timeout=10) == expected
    assert handle.state == "complete"
