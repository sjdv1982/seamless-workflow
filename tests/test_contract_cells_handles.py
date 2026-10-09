"""Bound projection / as_celltype handles: the anonymous-handle model of cells.md.

Written against the 2026-09-25 revision of contracts/cells.md (rulings rounds
1-7, register/cells-RULINGS.md), on points where both candidate revisions
agree.
"""
import pytest

from seamless import Buffer, Cell
from seamless.cell_errors import AuthorityError
from seamless import CacheMissError


def _plain(ctx, name="a", value=None):
    setattr(ctx, name, Cell("plain"))
    getattr(ctx, name).set({"x": 1, "y": [1, 2]} if value is None else value)
    ctx.compute(timeout=10)
    return getattr(ctx, name)


def _graph_edges_from(graph, ref):
    return [edge for edge in graph["connections"] if edge["source"] == ref]


# --- Reads through a handle ------------------------------------------------------

def test_projection_handle_read_evaluates_over_parent_checksum(make_context):
    """§Reads, Anonymous and projection handles: reads build and run the handle's Expression."""
    ctx = make_context()
    _plain(ctx)
    handle = ctx.a["x"]
    assert handle.checksum == Buffer(1, "plain").get_checksum()
    assert handle.value == 1
    assert handle.compute() == Buffer(1, "plain").get_checksum()


def test_handles_are_fresh_objects(make_context):
    """§Connecting, The handle and the node: `a = ctx.a; a[3] is a[3]` is False."""
    ctx = make_context()
    _plain(ctx)
    a = ctx.a
    assert a["x"] is not a["x"]
    assert a.x is not a.x


def test_fresh_handle_state_is_passive_and_local(make_context):
    ctx = make_context()
    _plain(ctx)
    handle = ctx.a["x"]
    assert ctx.a.state == "complete"
    assert handle.state == "waiting"
    assert handle.exception is None
    assert handle.checksum is not None
    assert handle.state == "complete"
    # Another fresh handle starts clean: the state is local to the handle.
    assert ctx.a["x"].state == "waiting"


@pytest.mark.parametrize("operation", [
    "checksum",
    "compute",
    "compute-timeout",
    "run",
])
def test_handle_without_parent_checksum_returns_none_without_raising(make_context, operation):
    ctx = make_context()
    ctx.u = Cell("plain")
    handle = ctx.u["x"]
    if operation == "checksum":
        result = handle.checksum
    elif operation == "compute":
        result = handle.compute()
    elif operation == "compute-timeout":
        result = handle.compute(timeout=1)
    else:
        result = handle.run()
    assert result is None
    assert handle.exception is None
    # Over an *unwired* parent, whether the handle reports `unwired` or `waiting` is deferred
    # (cells.md *Open questions*, "Handle state over an unwired parent"); pin neither.
    assert handle.state in ("waiting", "unwired")


def test_handle_compute_never_waits_on_a_progressing_parent(make_context, monkeypatch):
    import asyncio
    import time
    from threading import Event
    from seamless.checksum import expression as expression_module

    ctx = make_context()
    _plain(ctx, name="src", value={"a": {"x": 1}})
    entered, release = Event(), Event()
    original = expression_module._evaluate_expression_async

    async def gated(*args, **kwargs):
        entered.set()
        await asyncio.to_thread(release.wait, 15)
        return await original(*args, **kwargs)

    monkeypatch.setattr(expression_module, "_evaluate_expression_async", gated)
    try:
        ctx.p = ctx.src["a"]
        assert entered.wait(5)
        assert ctx.p.state == "waiting"
        handle = ctx.p["x"]
        start = time.monotonic()
        assert handle.compute(timeout=5) is None
        assert time.monotonic() - start < 2
        assert handle.checksum is None
        assert handle.exception is None
    finally:
        release.set()


def test_handle_failure_lives_on_that_handle_only(make_context):
    ctx = make_context()
    _plain(ctx)
    failing = ctx.a["nope"]
    assert failing.checksum is None
    assert failing.state == "failed"
    message = failing.exception
    assert isinstance(message, str) and message
    with pytest.raises(Exception):
        failing.run()
    fresh = ctx.a["nope"]
    assert fresh.exception is None
    assert fresh.state == "waiting"
    assert ctx.a.state == "complete"
    assert ctx.a.exception is None


def test_handle_memo_follows_parent_checksum_after_pathed_write(make_context):
    """§Writes through a handle: the next read after a write through the handle re-evaluates."""
    ctx = make_context()
    _plain(ctx)
    p = ctx.a.x
    assert p.value == 1
    p.set(10)
    assert p.value == 10
    p.value = 11
    assert p.value == 11
    p += 1
    assert ctx.a.value == {"x": 12, "y": [1, 2]}
    assert p.value == 12


# --- Writes through a handle -------------------------------------------------------

@pytest.mark.parametrize("method", [False, True])
def test_projection_handle_value_writes_are_pathed_parent_writes(make_context, method):
    """§Writes through a handle: retained and non-retained handles are one operation."""
    ctx = make_context()
    _plain(ctx)
    retained = ctx.a.y
    if method:
        retained.set([3])
        ctx.a.x.set(5)
    else:
        retained.value = [3]
        ctx.a.x.value = 5
    assert ctx.a.value == {"x": 5, "y": [3]}
    assert ctx.a.source is None
    assert ctx.get_graph()["connections"] == []


def test_set_checksum_with_input_celltype_converts_before_insertion(make_context):
    ctx = make_context()
    _plain(ctx)
    five = Buffer("5", "str")
    hold = five.tempref()
    try:
        ctx.a.x.set_checksum(five.get_checksum(), input_celltype="str")
        ctx.compute(timeout=10)
        assert ctx.a.value == {"x": "5", "y": [1, 2]}
    finally:
        hold.clear()


@pytest.mark.parametrize("form", ["checksum", "set_checksum"])
def test_unresolvable_checksum_write_raises_cache_miss_and_records_nothing(make_context, form):
    ctx = make_context()
    _plain(ctx)
    missing = Buffer({"never": "stored"}, "plain").get_checksum()
    with pytest.raises(CacheMissError):
        if form == "checksum":
            ctx.a.x.checksum = missing
        else:
            ctx.a.x.set_checksum(missing)
    assert ctx.a.value == {"x": 1, "y": [1, 2]}
    assert ctx.a.exception is None
    assert ctx.a.state == "complete"


@pytest.mark.parametrize("form", ["checksum", "buffer", "set_checksum"])
def test_clearing_a_sub_path_is_refused(make_context, form):
    ctx = make_context()
    _plain(ctx)
    with pytest.raises(ValueError) as info:
        if form == "set_checksum":
            ctx.a.x.set_checksum(None)
        else:
            setattr(ctx.a.x, form, None)
    message = str(info.value)
    assert "None" in message and "del" in message
    assert ctx.a.value == {"x": 1, "y": [1, 2]}


def test_storing_null_in_a_key_is_a_pathed_write(make_context):
    """§Null and None: `ctx.a.b = None` / `.set(None)` / `.value = None` store null in the key."""
    ctx = make_context()
    _plain(ctx)
    ctx.a.x = None
    assert ctx.a.value == {"x": None, "y": [1, 2]}
    ctx.a.y.set(None)
    assert ctx.a.value == {"x": None, "y": None}
    ctx.a.x.value = None
    assert ctx.a.state == "complete"


_AS_CELLTYPE_WRITES = {
    "set": lambda x: x.set(3),
    "value": lambda x: setattr(x, "value", 3),
    "set_buffer": lambda x: x.set_buffer(Buffer(3, "mixed")),
    "buffer": lambda x: setattr(x, "buffer", Buffer(3, "mixed")),
    "set_checksum": lambda x: x.set_checksum(Buffer(3, "mixed").get_checksum()),
    "checksum": lambda x: setattr(x, "checksum", Buffer(3, "mixed").get_checksum()),
}


def _iadd(x):
    x += 1


@pytest.mark.parametrize("form", [
    "set", "set_buffer", "set_checksum",
    "value", "buffer", "checksum",
    "iadd",
])
def test_writes_through_an_as_celltype_handle_raise_authority_error(make_context, form):
    ctx = make_context()
    _plain(ctx, value={"k": 1})
    x = ctx.a.as_celltype("mixed")
    with pytest.raises(AuthorityError):
        if form == "iadd":
            _iadd(x)
        else:
            _AS_CELLTYPE_WRITES[form](x)
    assert ctx.a.value == {"k": 1}


# --- Binding and assignment of handles -------------------------------------------------

@pytest.mark.parametrize("kind", ["projection", "as_celltype"])
def test_anonymous_handle_cannot_be_a_standalone_source(make_context, kind):
    """Clarity ruling (2026-09-26): capture of an anonymous/projection handle raises DependencyError."""
    from seamless_workflow.errors import DependencyError
    ctx = make_context()
    _plain(ctx)
    handle = ctx.a["x"] if kind == "projection" else ctx.a.as_celltype("mixed")
    with pytest.raises(DependencyError):
        Cell(source=handle)


def test_named_bound_node_remains_a_valid_standalone_source(make_context):
    """§Binding: a named bound source remains valid for Cell(source=...)."""
    ctx = make_context()
    _plain(ctx)
    assert Cell(source=ctx.a).value == {"x": 1, "y": [1, 2]}


@pytest.mark.parametrize("kind", ["projection", "as_celltype"])
def test_anonymous_handle_cannot_be_assigned_into_another_context(make_context, kind):
    """Clarity ruling (2026-09-26): cross-Context assignment of a handle raises DependencyError, as for named nodes."""
    from seamless_workflow.errors import DependencyError
    ctx = make_context()
    _plain(ctx)
    other = make_context()
    handle = ctx.a["x"] if kind == "projection" else ctx.a.as_celltype("mixed")
    with pytest.raises(DependencyError):
        other.z = handle
    # Assigning it into its own Context instead is the valid spelling.
    ctx.own = handle
    ctx.compute(timeout=10)
    assert ctx.own.value == (1 if kind == "projection" else {"x": 1, "y": [1, 2]})


def test_assigning_to_a_new_name_feeds_it_through_a_dummy_edge(make_context):
    """cells.md, *Assigning an anonymous handle*: "A new name is fed through a dummy
    edge. `x = ctx.b.as_celltype("plain"); ctx.a = x`, where `ctx.a` did not exist,
    creates the named node `a` with the handle's `celltype` [...] **Nothing is renamed
    and no handle is invalidated**: `x` stays a handle to its anonymous cell, every
    other handle to the same recipe stays valid, and a later `ctx.d = x` feeds `d` the
    same way." And: "For a handle whose link reads directly from a named node, such as
    `ctx.b[3]` or `ctx.b.as_celltype("plain")`, `get_graph()` writes that link as `a`'s
    own incoming edge"."""
    from seamless_workflow.errors import StaleWorkflowHandleError
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.compute(timeout=10)
    x = ctx.b.as_celltype("plain")
    y = ctx.b.as_celltype("plain")
    ctx.a = x
    ctx.compute(timeout=10)
    assert ctx.a.celltype == "plain"
    assert ctx.a.value == [10, 20, 30, 40]
    # Both handles stay valid anonymous handles over b.
    assert x.celltype == y.celltype == "plain"
    assert x.path == y.path == ""
    assert x.value == y.value == [10, 20, 30, 40]
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert sorted(node["path"] for node in graph["nodes"]) == [["a"], ["b"]]
    assert graph["connections"] == [{"type": "connection", "target": ["a"], "source": ["b"]}]
    # A later ctx.d = x feeds d the same way: its own direct edge from b.
    ctx.d = x
    ctx.compute(timeout=10)
    assert ctx.d.celltype == "plain"
    assert ctx.d.value == [10, 20, 30, 40]
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert sorted((edge["target"], edge["source"]) for edge in graph["connections"]) == [
        (["a"], ["b"]),
        (["d"], ["b"]),
    ]
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.a.value == restored.d.value == [10, 20, 30, 40]
    assert restored.get_graph() == graph
    # x was never a handle to `a`: deleting `a` leaves x and y reading b.
    del ctx.a
    assert x.value == y.value == [10, 20, 30, 40]
    # StaleWorkflowHandleError remains for a handle to a deleted node.
    del ctx.b
    with pytest.raises(StaleWorkflowHandleError):
        _ = x.value


def test_assigning_to_an_existing_name_adds_an_edge_from_the_symbol(make_context):
    """cells.md, *Assigning an anonymous handle*: "`ctx.a = x`, where `ctx.a` already
    exists, adds an edge from `x`'s symbol to `a`. `a` keeps its own `celltype` and
    converts into it [...] `x` stays exactly what it was: an anonymous handle. A later
    `ctx.d = x` therefore adds another edge from the same symbol." On save, *Which
    entries are serialized*: the identity edge into the new `d` "is saved as its
    target's own incoming link", while the entry stays because the edge into
    `existing` names it."""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.existing = Cell("mixed")
    ctx.compute(timeout=10)
    x = ctx.b.as_celltype("plain")
    ctx.existing = x
    ctx.d = x
    ctx.compute(timeout=10)
    assert ctx.existing.celltype == "mixed"
    assert ctx.existing.value == ctx.d.value == [10, 20, 30, 40]
    graph = ctx.get_graph()
    symbols = list(graph["anonymous_nodes"])
    assert len(symbols) == 1
    assert graph["anonymous_nodes"][symbols[0]] == {
        "source": {"node": ["b"]}, "celltype": "plain", "path": "",
    }
    targets = sorted(edge["target"] for edge in _graph_edges_from(graph, {"symbol": symbols[0]}))
    assert targets == [["existing"]]
    assert [edge["source"] for edge in graph["connections"] if edge["target"] == ["d"]] == [["b"]]
    # x is still an anonymous handle, not a handle to `existing` or `d`.
    assert x.celltype == "plain"
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.existing.value == restored.d.value == [10, 20, 30, 40]
    assert restored.get_graph() == graph


def test_held_as_celltype_handle_creates_no_entry(make_context):
    """cells.md, *The handle and the node*: "A handle neither creates nor holds the
    node. Evaluating `ctx.b.as_celltype("plain")` creates a handle to a recipe, and
    nothing in the graph. The anonymous cell is created [...] when an edge or another
    entry first refers to that recipe." *Which entries are serialized*: "An entry held
    only by a live handle is runtime state and is excluded"."""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[1]")
    held = ctx.b.as_celltype("plain")
    graph = ctx.get_graph()
    assert graph["__seamless_workflow__"] == "0.6"
    assert graph["anonymous_nodes"] == {}
    assert held.celltype == "plain"
    assert held.value == [1]
    assert ctx.get_graph()["anonymous_nodes"] == {}
    # An edge into a cell of a different celltype is not an identity, so the entry
    # it names is saved.
    ctx.existing = Cell("mixed")
    ctx.existing = held
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    (symbol, entry), = graph["anonymous_nodes"].items()
    assert entry == {"source": {"node": ["b"]}, "celltype": "plain", "path": ""}
    assert [edge["target"] for edge in _graph_edges_from(graph, {"symbol": symbol})] == [
        ["existing"]
    ]


def test_held_projection_handle_creates_no_entry(make_context):
    """cells.md, *The handle and the node*: "A handle neither creates nor holds the
    node." *Which entries are serialized*: "An entry held only by a live handle is
    runtime state and is excluded", and a single link's identity edge is saved as the
    target's own incoming link, with "the entry only if something else names it"."""
    ctx = make_context()
    ctx.b = Cell("plain")
    ctx.b.set({"x": [1, 2]})
    held = ctx.b["x"]
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert held.value == [1, 2]
    assert ctx.get_graph()["anonymous_nodes"] == {}
    # A projection link may not also convert (cells.md, *Connecting*), so its edge
    # into a cell is an identity: saved as the cell's own link, with no entry.
    ctx.existing = Cell("plain")
    ctx.existing = held
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert [edge["source"] for edge in graph["connections"] if edge["target"] == ["existing"]] == [
        ["b", "x"]
    ]
    # Another entry that names the link -- a chain starting with it -- saves it.
    ctx.chained = held.as_celltype("mixed")
    ctx.compute(timeout=10)
    assert ctx.chained.value == [1, 2]
    entries = ctx.get_graph()["anonymous_nodes"]
    assert {"source": {"node": ["b"]}, "celltype": "plain", "path": "x"} in entries.values()


def test_held_projected_as_celltype_handle_creates_no_entry(make_context):
    """cells.md, *The handle and the node*: "A handle neither creates nor holds the
    node." Once an edge refers to the chain, "every link of the chain is an anonymous
    cell" and *Which entries are serialized*: "`get_graph()` includes every entry that
    an edge, or another serialized entry, names"."""
    ctx = make_context()
    ctx.b = Cell("mixed")
    ctx.b.set({"a": [1, 2]})
    held = ctx.b["a"].as_celltype("plain")
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert held.value == [1, 2]
    assert ctx.get_graph()["anonymous_nodes"] == {}
    ctx.existing = Cell("mixed")
    ctx.existing = held
    ctx.compute(timeout=10)
    assert ctx.existing.value == [1, 2]
    graph = ctx.get_graph()
    assert len(graph["anonymous_nodes"]) == 2
    path_symbol, path_entry = next(
        (symbol, entry)
        for symbol, entry in graph["anonymous_nodes"].items()
        if entry["path"] == "a"
    )
    conversion_symbol, conversion_entry = next(
        (symbol, entry)
        for symbol, entry in graph["anonymous_nodes"].items()
        if entry["path"] == ""
    )
    assert path_entry == {
        "source": {"node": ["b"]},
        "celltype": "mixed",
        "path": "a",
    }
    assert conversion_entry == {
        "source": {"symbol": path_symbol},
        "celltype": "plain",
        "path": "",
    }
    assert [edge["target"] for edge in _graph_edges_from(graph, {"symbol": conversion_symbol})] == [
        ["existing"]
    ]

    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.existing.value == [1, 2]
    restored_held = restored.b["a"].as_celltype("plain")
    assert restored_held.value == [1, 2]
    assert restored.get_graph()["anonymous_nodes"] == graph["anonymous_nodes"]


def test_held_transformer_result_handle_does_not_break_graph_reload(make_context):
    """Review finding 7. workflow-context.md, *Graph serialization*: "`ctx.get_graph()`
    returns the durable graph [...] never runtime state", and "An entry held only by a
    live handle is runtime state and is excluded: after a reload nothing could reach
    it"."""
    def func(a):
        return {"x": a, "y": [a, a]}

    ctx = make_context()
    ctx.inp = Cell("int")
    ctx.inp.set(3)
    ctx.tf = func
    ctx.tf.pins.a = ctx.inp
    ctx.compute(timeout=30)
    held = ctx.tf.result["x"]
    assert held.value == 3
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=30)
    assert restored.tf.result.value == {"x": 3, "y": [3, 3]}
    assert restored.get_graph() == graph


def test_entry_whose_source_is_a_transformer_reloads(make_context):
    """cells.md, *Anonymous cells*: an anonymous cell's source is "its parent: a named
    node or another anonymous cell" -- a transformer is a named node.
    workflow-context.md, *Graph serialization*: `set_graph` reloads what `get_graph`
    wrote."""
    def func(a):
        return {"x": a, "y": [a, a]}

    ctx = make_context()
    ctx.inp = Cell("int")
    ctx.inp.set(3)
    ctx.tf = func
    ctx.tf.pins.a = ctx.inp
    ctx.out = Cell("mixed")
    ctx.out = ctx.tf.result.as_celltype("plain")
    ctx.compute(timeout=30)
    assert ctx.out.value == {"x": 3, "y": [3, 3]}
    graph = ctx.get_graph()
    (symbol, entry), = graph["anonymous_nodes"].items()
    assert entry == {"source": {"node": ["tf"]}, "celltype": "plain", "path": ""}
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=30)
    assert restored.out.value == {"x": 3, "y": [3, 3]}
    assert restored.get_graph() == graph


def test_graph_does_not_depend_on_live_handles(make_context):
    """workflow-context.md, *Graph serialization*: "`ctx.get_graph()` returns the durable
    graph [...] never runtime state", and "An entry held only by a live handle is runtime
    state and is excluded". The graph is the same before a handle exists, while it is
    alive, and after it is gone."""
    import gc

    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.a = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    before = ctx.get_graph()
    assert before["anonymous_nodes"]
    handles = [
        ctx.b.as_celltype("mixed"),
        ctx.b[0],
        ctx.b.as_celltype("plain")[1],
        ctx.b[2].as_celltype("plain"),
    ]
    values = [handle.value for handle in handles]
    assert values[1:3] == ["[", 20]
    during = ctx.get_graph()
    del handles
    gc.collect()
    after = ctx.get_graph()
    assert before == during == after


@pytest.mark.parametrize("target", ["cell", "join slot", "pin"])
def test_identity_edge_from_a_single_link_is_saved_as_the_targets_own_link(make_context, target):
    """cells.md, *Which entries are serialized*: "A dummy edge from a single link is
    saved as its target's own incoming link. When an edge comes from an anonymous cell
    whose link reads directly from a named node, and the edge is an identity (the
    celltype at the target -- a cell's, a join slot's or a pin's -- equals the anonymous
    cell's), `get_graph()` writes that link as the target's incoming edge, and writes
    the entry only if something else names it"."""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    handle = ctx.b.as_celltype("plain")
    if target == "cell":
        ctx.t = Cell("plain")
        ctx.t = handle
        target_path = ["t"]
        expected = [10, 20, 30, 40]

        def read(c):
            return c.t.value
    elif target == "join slot":
        ctx.t = Cell("plain")
        ctx.t.set({})
        ctx.t["k"] = handle
        target_path = ["t", "k"]
        expected = {"k": [10, 20, 30, 40]}

        def read(c):
            return c.t.value
    else:
        def identity(p):
            return p

        ctx.tf = identity
        ctx.tf.celltypes.p = "plain"
        ctx.tf.pins.p = handle
        target_path = ["tf", "p"]
        expected = [10, 20, 30, 40]

        def read(c):
            return c.tf.result.value
    ctx.compute(timeout=30)
    assert read(ctx) == expected
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert [edge["source"] for edge in graph["connections"] if edge["target"] == target_path] == [
        ["b"]
    ]
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=30)
    assert read(restored) == expected
    assert restored.get_graph() == graph


@pytest.mark.parametrize("interleave", [False, True])
@pytest.mark.parametrize("order", ["first-second", "second-first"])
@pytest.mark.parametrize("source_celltype", ["text", "mixed"])
def test_colliding_symbols_are_suffixed_in_arrival_order(
    make_context, monkeypatch, source_celltype, order, interleave
):
    """cells.md, *Symbols*: "An anonymous cell's symbol is five hex characters derived
    from `(source, celltype, path)`, assigned when the anonymous cell is created [...]
    Two *different* recipes whose five characters collide are suffixed `-1`, `-2`, ...
    in the order they were added." Saving in between assigns nothing, and whether the
    link is evaluated (`text -> plain`) or elided (`mixed -> plain`, *Elidable and
    elided*) makes no difference."""
    import seamless_workflow.graph as graph_module

    monkeypatch.setattr(graph_module, "_symbol_base", lambda recipe: "abcde")
    ctx = make_context()
    ctx.first = Cell(source_celltype)
    ctx.first.set("[1, 2]" if source_celltype == "text" else [1, 2])
    ctx.second = Cell(source_celltype)
    ctx.second.set("[3, 4]" if source_celltype == "text" else [3, 4])
    ctx.to_first = Cell("mixed")
    ctx.to_second = Cell("mixed")
    names = order.split("-")
    for name in names:
        setattr(ctx, "to_" + name, getattr(ctx, name).as_celltype("plain"))
        if interleave:
            ctx.get_graph()
    ctx.compute(timeout=10)
    assert ctx.to_first.value == [1, 2]
    assert ctx.to_second.value == [3, 4]
    graph = ctx.get_graph()
    symbol_of = {
        entry["source"]["node"][0]: symbol for symbol, entry in graph["anonymous_nodes"].items()
    }
    assert symbol_of == {names[0]: "abcde", names[1]: "abcde-1"}
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.to_first.value == [1, 2]
    assert restored.get_graph() == graph


def test_a_collapsed_link_still_claims_its_symbol(make_context, monkeypatch):
    """cells.md, *Symbols*: a symbol is "assigned when the anonymous cell is created"
    and "never changes once assigned". The anonymous cell behind an edge that is saved
    as its target's own link (*Which entries are serialized*) is still created, so it
    claims its symbol before a later colliding recipe, and that symbol is the one a
    later save shows."""
    import seamless_workflow.graph as graph_module

    monkeypatch.setattr(graph_module, "_symbol_base", lambda recipe: "abcde")
    ctx = make_context()
    ctx.first = Cell("text")
    ctx.first.set("[1, 2]")
    ctx.second = Cell("text")
    ctx.second.set("[3, 4]")
    # An identity edge from first's link: collapsed on save, but the cell exists.
    ctx.new = ctx.first.as_celltype("plain")
    ctx.to_second = Cell("mixed")
    ctx.to_second = ctx.second.as_celltype("plain")
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {
        "abcde-1": {"source": {"node": ["second"]}, "celltype": "plain", "path": ""},
    }
    # A later edge that saves first's entry shows the symbol it got at creation.
    ctx.to_first = Cell("mixed")
    ctx.to_first = ctx.first.as_celltype("plain")
    ctx.compute(timeout=10)
    assert ctx.get_graph()["anonymous_nodes"] == {
        "abcde": {"source": {"node": ["first"]}, "celltype": "plain", "path": ""},
        "abcde-1": {"source": {"node": ["second"]}, "celltype": "plain", "path": ""},
    }


@pytest.mark.parametrize("target_exists", [False, True])
def test_projected_as_celltype_assignment_roundtrips_through_outermost_symbol(
    make_context, target_exists
):
    ctx = make_context()
    ctx.b = Cell("mixed")
    ctx.b.set({"a": [1, 2]})
    if target_exists:
        ctx.target = Cell("plain")
    held = ctx.b["a"].as_celltype("plain")
    target_name = "target" if target_exists else "new_target"
    setattr(ctx, target_name, held)
    ctx.compute(timeout=10)
    assert getattr(ctx, target_name).value == [1, 2]

    graph = ctx.get_graph()
    edge = next(
        edge for edge in graph["connections"] if edge["target"] == [target_name]
    )
    assert set(edge["source"]) == {"symbol"}
    outer_symbol = edge["source"]["symbol"]
    outer_entry = graph["anonymous_nodes"][outer_symbol]
    assert outer_entry["path"] == ""
    assert outer_entry["celltype"] == "plain"
    assert set(outer_entry["source"]) == {"symbol"}

    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert getattr(restored, target_name).value == [1, 2]


def test_same_recipe_shares_one_symbol_and_entries_use_tagged_refs(make_context):
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.p = ctx.b.as_celltype("plain")[0]
    ctx.q = ctx.b.as_celltype("plain")[1]
    ctx.compute(timeout=10)
    assert (ctx.p.value, ctx.q.value) == (10, 20)
    graph = ctx.get_graph()
    entries = graph["anonymous_nodes"]
    assert len(entries) == 3
    (conversion_symbol,), = [
        (symbol,)
        for symbol, entry in entries.items()
        if entry == {"source": {"node": ["b"]}, "celltype": "plain", "path": ""}
    ]
    path_entries = {
        entry["path"]: (symbol, entry)
        for symbol, entry in entries.items()
        if entry["path"] in {"[0]", "[1]"}
    }
    assert set(path_entries) == {"[0]", "[1]"}
    for symbol, entry in path_entries.values():
        assert entry == {
            "source": {"symbol": conversion_symbol},
            "celltype": "plain",
            "path": entry["path"],
        }
        assert [edge["target"] for edge in _graph_edges_from(graph, {"symbol": symbol})] == [
            ["p"] if entry["path"] == "[0]" else ["q"]
        ]


def test_symbol_is_stable_across_value_changes_of_its_source(make_context):
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.result = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    before = set(ctx.get_graph()["anonymous_nodes"])
    ctx.b.set("[50, 60, 70, 80]")
    ctx.compute(timeout=10)
    assert ctx.result.value == 80
    assert set(ctx.get_graph()["anonymous_nodes"]) == before


@pytest.mark.parametrize("kind", ["item", "attribute", "slice"])
def test_projection_handle_starts_non_scratch(make_context, kind):
    """§Scratch policy: a projection is a new Cell that owns its own result, so it starts non-scratch."""
    ctx = make_context()
    _plain(ctx)
    ctx.a.scratch = True
    handle = {"item": lambda: ctx.a["y"], "attribute": lambda: ctx.a.y, "slice": lambda: ctx.a.y[0:1]}[kind]()
    assert ctx.a.scratch is True
    assert handle.scratch is False


# --- Round 8 rulings (register/cells-RULINGS.md) ----------------------------------------

def test_handle_follows_its_parent_when_the_parent_is_retyped(make_context):
    """cells.md, *The input*: "**A bound handle's `celltype` follows its parent.**
    Retyping the parent retypes every live handle over it, so a retype never leaves
    a bound handle `miswired`; the celltype is fixed only when an edge creates the
    handle's anonymous cell." (This replaces round 8, ruling 1, which made the
    handle `miswired`.)"""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.compute(timeout=10)
    handle = ctx.b[3]
    assert handle.celltype == "text"
    assert handle.value == ","
    ctx.b.celltype = "plain"
    ctx.compute(timeout=10)
    assert handle.celltype == "plain"
    assert handle.value == 40
    assert handle.state == "complete"
    assert handle.checksum == Buffer(40, "plain").get_checksum()
    ctx.b.celltype = "text"
    ctx.compute(timeout=10)
    assert handle.celltype == "text"
    assert handle.value == ","


@pytest.mark.parametrize("source_celltype, elided", [("mixed", True), ("text", False)])
def test_conversion_then_path_is_elided_only_when_checksum_preserving(make_context, monkeypatch, source_celltype, elided):
    """Round 8, ruling 2: `ctx.a = ctx.b.as_celltype("plain")[3]` elides the anonymous
    conversion only when b -> plain preserves the checksum (mixed); from text it is a
    fusion barrier, and the Context builds and evaluates the intermediate."""
    from seamless import Expression
    ctx = make_context()
    ctx.b = Cell(source_celltype)
    ctx.b.set([10, 20, 30, 40] if source_celltype == "mixed" else "[10, 20, 30, 40]")
    ctx.compute(timeout=10)
    built = []
    initialize = Expression.__post_init__

    def record(expression):
        initialize(expression)
        built.append((expression.input_celltype, expression.celltype, expression.path))

    monkeypatch.setattr(Expression, "__post_init__", record)
    ctx.a = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    assert ctx.a.value == 40
    intermediate = (source_celltype, "plain", "")
    assert (intermediate in built) is (not elided)
    # Named and anonymous intermediates build the same recipe either way.
    ctx.mid = Cell("plain")
    ctx.mid = ctx.b
    ctx.named = ctx.mid[3]
    ctx.compute(timeout=10)
    assert ctx.named.value == 40
    assert ctx.named.build().identity_key == ctx.a.build().identity_key
